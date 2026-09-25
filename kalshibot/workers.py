"""Strategy workers, each on its own clock, under a supervisor.

Until now everything happened in one sequential sweep: page eighty thousand
markets, rank them, spend the order book budget, run every strategy, trade,
sleep. One pass of that took **2.8 hours** in the first live run, and twenty
minutes of it was paging the exchange to reach four hundred markets whose
tickers the forecast strategy already knew.

The cure is not to make the sweep faster. It is to stop making a strategy that
needs six series wait on a strategy that needs the whole exchange.

    supervisor
      +-- forecast worker    every  2 min   6 named series
      +-- arbitrage worker   every 20 min   the whole exchange
      +-- position worker    every 30 sec   the tickers we hold

WHAT IS NOT SPLIT
-----------------
Risk. The limits are TOTAL exposure, per-event, per-market, and two workers
that each believe they own the account cannot enforce any of them. Every
worker runs in this one process, judges through the same gate, and executes
through the same manager holding the same lock. Splitting the work is fine;
splitting the account is not.

FAILURE ISOLATION
-----------------
A worker that raises is caught, recorded, and backed off - it never takes down
the process or another worker. A worker that keeps failing is SUSPENDED rather
than retried at full speed, because a data source that has been down for an
hour is not going to be fixed by asking it every two minutes, and a log full
of the same error is a log nobody reads.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class WorkerConfig:
    name: str = "worker"
    #: seconds between passes
    interval: float = 120.0
    enabled: bool = True
    #: consecutive failures before the worker suspends itself
    max_consecutive_failures: int = 5
    #: after a failure, wait this much longer each time, up to the cap
    backoff_start: float = 5.0
    backoff_max: float = 600.0
    #: a pass that runs longer than this is reported. Not killed - there is no
    #: safe way to interrupt a worker mid-trade - but it is said out loud.
    slow_pass_seconds: float = 900.0
    #: how often to say out loud that the worker is deliberately doing
    #: nothing. Silence and death look identical from outside.
    idle_log_seconds: float = 300.0

    @classmethod
    def from_dict(cls, name: str, d: dict | None, default_interval: float) -> "WorkerConfig":
        d = d or {}
        return cls(
            name=name,
            interval=float(d.get("interval_seconds", default_interval)),
            enabled=bool(d.get("enabled", True)),
            max_consecutive_failures=int(d.get("max_consecutive_failures", 5)),
            backoff_start=float(d.get("backoff_start_seconds", 5.0)),
            backoff_max=float(d.get("backoff_max_seconds", 600.0)),
            slow_pass_seconds=float(d.get("slow_pass_seconds", 900.0)),
            idle_log_seconds=float(d.get("idle_log_seconds", 300.0)),
        )


@dataclass
class WorkerHealth:
    name: str
    passes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    suspended: bool = False
    suspended_reason: str = ""
    last_started: datetime | None = None
    last_finished: datetime | None = None
    last_seconds: float = 0.0
    slowest_seconds: float = 0.0
    last_error: str = ""
    running: bool = False
    #: passes skipped because the gate said "not now" - e.g. a closed exchange
    skips: int = 0
    last_skip_reason: str = ""
    idle_since: datetime | None = None

    @property
    def ok(self) -> bool:
        return not self.suspended and self.consecutive_failures == 0

    @property
    def idle(self) -> bool:
        return self.idle_since is not None

    def summary(self) -> str:
        if self.suspended:
            return f"{self.name}: SUSPENDED after {self.failures} failures - {self.last_error}"
        if self.idle:
            mins = (_now() - self.idle_since).total_seconds() / 60.0
            return (f"{self.name}: WAITING {mins:.0f} min - "
                    f"{self.last_skip_reason or 'gate said not now'}")
        state = "running" if self.running else "idle"
        bits = [f"{self.name}: {state}", f"{self.passes} passes"]
        if self.last_seconds:
            bits.append(f"last {self.last_seconds:.0f}s")
        if self.failures:
            bits.append(f"{self.failures} failures")
        if self.consecutive_failures:
            bits.append(f"{self.consecutive_failures} in a row")
        return ", ".join(bits)


class Worker:
    """One job, on its own clock, isolated from every other job."""

    def __init__(self, cfg: WorkerConfig, job, *, should_run=None):
        self.cfg = cfg
        self.job = job
        #: optional gate, e.g. "is the exchange open?". Returning False skips
        #: the pass without counting it as a failure.
        self.should_run = should_run
        self.health = WorkerHealth(name=cfg.name)
        self._backoff = 0.0
        self._idle_logged_at: datetime | None = None

    # ------------------------------------------------------------------
    def run_once(self) -> bool:
        """One pass. Returns False if it failed. Never raises."""
        if self.health.suspended:
            return False
        if self.should_run is not None:
            try:
                answer = self.should_run()
            except Exception as exc:  # noqa: BLE001
                log.debug("WORKER  %s gate check failed: %s", self.cfg.name, exc)
            else:
                allowed, why = (answer if isinstance(answer, tuple)
                                else (answer, ""))
                if not allowed:
                    self._on_skip(why)
                    return True
                self._on_resume()

        self.health.running = True
        self.health.last_started = _now()
        started = time.time()
        try:
            self.job()
        except Exception as exc:  # noqa: BLE001
            self._on_failure(exc)
            return False
        else:
            elapsed = time.time() - started
            self.health.passes += 1
            self.health.consecutive_failures = 0
            self.health.last_seconds = elapsed
            self.health.slowest_seconds = max(self.health.slowest_seconds, elapsed)
            self._backoff = 0.0
            if elapsed > self.cfg.slow_pass_seconds:
                log.warning("WORKER  %s took %.0fs, over its %.0fs budget",
                            self.cfg.name, elapsed, self.cfg.slow_pass_seconds)
            return True
        finally:
            self.health.running = False
            self.health.last_finished = _now()

    def _on_skip(self, why: str) -> None:
        """Deliberately doing nothing - and saying so.

        This is the failure the worker refactor introduced. The old single
        loop printed "EXCHANGE CLOSED, checking again in 15 min" every time it
        waited. Moving to workers turned that into a gate that returns False
        and logs nothing, so a bot waiting for the market to open looked
        exactly like a bot that had died - which is precisely how it was read
        on 19 September, after fifteen silent minutes.

        A worker that is idle on purpose must say so on a clock of its own.
        """
        self.health.skips += 1
        self.health.last_skip_reason = why or "gate said not now"
        now = _now()
        if self.health.idle_since is None:
            self.health.idle_since = now
            self._idle_logged_at = now
            log.info("WORKER  %s is WAITING, not broken: %s. Next check in "
                     "%.0fs.", self.cfg.name, self.health.last_skip_reason,
                     self.cfg.interval)
            return
        last = getattr(self, "_idle_logged_at", None)
        if last is None or (now - last).total_seconds() >= self.cfg.idle_log_seconds:
            self._idle_logged_at = now
            mins = (now - self.health.idle_since).total_seconds() / 60.0
            log.info("WORKER  %s still waiting after %.0f min (%d checks): %s",
                     self.cfg.name, mins, self.health.skips,
                     self.health.last_skip_reason)

    def _on_resume(self) -> None:
        if self.health.idle_since is None:
            return
        mins = (_now() - self.health.idle_since).total_seconds() / 60.0
        log.info("WORKER  %s resuming after %.0f min of waiting",
                 self.cfg.name, mins)
        self.health.idle_since = None
        self._idle_logged_at = None

    def _on_failure(self, exc: Exception) -> None:
        self.health.failures += 1
        self.health.consecutive_failures += 1
        self.health.last_error = f"{type(exc).__name__}: {exc}"
        self._backoff = (self.cfg.backoff_start if not self._backoff
                         else min(self._backoff * 2, self.cfg.backoff_max))
        if self.health.consecutive_failures >= self.cfg.max_consecutive_failures:
            self.health.suspended = True
            self.health.suspended_reason = self.health.last_error
            log.error("WORKER  %s SUSPENDED after %d consecutive failures: %s",
                      self.cfg.name, self.health.consecutive_failures,
                      self.health.last_error)
            log.error("WORKER  the other workers carry on. Fix the cause and "
                      "restart the bot to bring %s back.", self.cfg.name)
        else:
            log.error("WORKER  %s failed (%d in a row), retrying in %.0fs: %s",
                      self.cfg.name, self.health.consecutive_failures,
                      self._backoff, self.health.last_error, exc_info=True)

    def resume(self) -> None:
        """Bring a suspended worker back. Only ever called deliberately."""
        self.health.suspended = False
        self.health.suspended_reason = ""
        self.health.consecutive_failures = 0
        self._backoff = 0.0

    # ------------------------------------------------------------------
    def run_forever(self, stop: threading.Event) -> None:
        log.info("WORKER  %s started, every %.0fs", self.cfg.name, self.cfg.interval)
        while not stop.is_set():
            self.run_once()
            if self.health.suspended:
                # Nothing left to do. Wait for the stop rather than spinning.
                stop.wait()
                break
            if stop.wait(self._backoff or self.cfg.interval):
                break
        log.info("WORKER  %s stopped", self.cfg.name)


class Supervisor:
    """Owns the worker threads and can say how each one is doing."""

    def __init__(self):
        self.workers: list[Worker] = []
        self.threads: dict[str, threading.Thread] = {}
        self.stop = threading.Event()

    def add(self, worker: Worker) -> Worker:
        if not worker.cfg.enabled:
            log.info("WORKER  %s is disabled", worker.cfg.name)
            return worker
        self.workers.append(worker)
        return worker

    def start(self) -> None:
        for worker in self.workers:
            t = threading.Thread(target=worker.run_forever, args=(self.stop,),
                                 name=worker.cfg.name, daemon=True)
            self.threads[worker.cfg.name] = t
            t.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        self.stop.set()
        for name, t in self.threads.items():
            t.join(timeout=timeout)
            if t.is_alive():
                # Daemon threads, so this does not hold the process open. Say
                # so rather than pretending the shutdown was clean.
                log.warning("WORKER  %s did not stop within %.0fs", name, timeout)

    # ------------------------------------------------------------------
    def health(self) -> list[WorkerHealth]:
        return [w.health for w in self.workers]

    def suspended(self) -> list[str]:
        return [w.cfg.name for w in self.workers if w.health.suspended]

    def waiting(self) -> list[str]:
        return [w.cfg.name for w in self.workers if w.health.idle]

    def all_waiting(self) -> bool:
        return bool(self.workers) and all(
            w.health.idle or w.health.suspended for w in self.workers)

    def summary(self) -> str:
        if not self.workers:
            return "no workers"
        return " | ".join(w.health.summary() for w in self.workers)

    def check_threads(self) -> list[str]:
        """Any worker whose thread has died without being asked to stop.

        A thread that vanished is worse than one that is failing: it makes no
        noise at all, and the job it was doing simply stops happening.
        """
        dead = []
        for worker in self.workers:
            t = self.threads.get(worker.cfg.name)
            if t is not None and not t.is_alive() and not self.stop.is_set():
                dead.append(worker.cfg.name)
                log.error("WORKER  %s thread is gone and was not asked to "
                          "stop; %s is no longer happening",
                          worker.cfg.name, worker.cfg.name)
        return dead
