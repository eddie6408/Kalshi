"""Strategy workers and the supervisor over them.

The point of splitting the work is that a strategy needing six series should
not wait on one needing the whole exchange. The point of the SUPERVISOR is
narrower and more important: when one of them breaks, exactly one of them
stops.

So these tests are mostly about what happens when a worker goes wrong.
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.workers import Supervisor, Worker, WorkerConfig


def worker(job, name="w", **cfg):
    base = {"name": name, "interval": 0.01, "backoff_start": 0.01,
            "backoff_max": 0.02}
    base.update(cfg)
    return Worker(WorkerConfig(**base), job)


def counter():
    calls = []
    return calls, lambda: calls.append(1)


def exploder(message="data source down"):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError(message)
    return calls, boom


# ------------------------- one pass -------------------------
def test_a_good_pass_is_recorded():
    calls, job = counter()
    w = worker(job)
    assert w.run_once() is True
    assert calls == [1]
    assert w.health.passes == 1 and w.health.failures == 0
    assert w.health.ok is True


def test_a_failing_pass_never_raises_out_of_the_worker():
    _, job = exploder()
    w = worker(job)
    assert w.run_once() is False        # returns, does not raise
    assert w.health.failures == 1
    assert "data source down" in w.health.last_error


def test_a_gate_can_skip_a_pass_without_calling_it_a_failure():
    # "Is the exchange open?" is not a fault.
    calls, job = counter()
    w = Worker(WorkerConfig(name="w", interval=0.01), job,
               should_run=lambda: False)
    assert w.run_once() is True
    assert calls == []
    assert w.health.failures == 0 and w.health.passes == 0


def test_a_broken_gate_does_not_stop_the_work():
    calls, job = counter()

    def bad_gate():
        raise RuntimeError("status endpoint down")
    w = Worker(WorkerConfig(name="w", interval=0.01), job, should_run=bad_gate)
    w.run_once()
    assert calls == [1], "a broken gate must not silently stop the job"


# ------------------------- backoff and suspension -------------------------
def test_repeated_failures_suspend_the_worker():
    # A data source down for an hour is not fixed by asking every two minutes,
    # and a log full of the same error is a log nobody reads.
    _, job = exploder()
    w = worker(job, max_consecutive_failures=3)
    for _ in range(3):
        w.run_once()
    assert w.health.suspended is True
    assert w.health.consecutive_failures == 3


def test_a_suspended_worker_does_no_more_work():
    calls, job = exploder()
    w = worker(job, max_consecutive_failures=2)
    w.run_once(); w.run_once()
    assert w.health.suspended
    w.run_once(); w.run_once()
    assert len(calls) == 2, "a suspended worker kept running"


def test_a_success_clears_the_streak():
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise RuntimeError("transient")
    w = worker(flaky, max_consecutive_failures=5)
    w.run_once(); w.run_once()
    assert w.health.consecutive_failures == 2
    state["fail"] = False
    w.run_once()
    assert w.health.consecutive_failures == 0
    assert w.health.failures == 2, "the history is kept, only the streak resets"
    assert w.health.ok is True


def test_backoff_grows_and_is_capped():
    _, job = exploder()
    w = worker(job, max_consecutive_failures=99, backoff_start=1.0, backoff_max=4.0)
    seen = []
    for _ in range(5):
        w.run_once()
        seen.append(w._backoff)
    assert seen[0] == 1.0 and seen[1] == 2.0 and seen[2] == 4.0
    assert max(seen) <= 4.0


def test_a_suspended_worker_can_be_resumed_deliberately():
    _, job = exploder()
    w = worker(job, max_consecutive_failures=1)
    w.run_once()
    assert w.health.suspended
    w.resume()
    assert w.health.suspended is False and w.health.consecutive_failures == 0


def test_a_slow_pass_is_reported_but_not_killed():
    # There is no safe way to interrupt a worker mid-trade, so a slow pass is
    # said out loud rather than cut off.
    def slow():
        time.sleep(0.05)
    w = worker(slow, slow_pass_seconds=0.001)
    assert w.run_once() is True
    assert w.health.last_seconds >= 0.05
    assert w.health.slowest_seconds >= 0.05


# ------------------------- the loop -------------------------
def test_the_loop_runs_repeatedly_and_stops_when_told():
    calls = []
    twice = threading.Event()

    def job():
        calls.append(1)
        if len(calls) >= 2:
            twice.set()
    w = worker(job)
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()
    try:
        assert twice.wait(timeout=10)
    finally:
        stop.set()
    t.join(timeout=10)
    assert not t.is_alive()


def test_a_suspended_worker_waits_instead_of_spinning():
    calls, job = exploder()
    w = worker(job, max_consecutive_failures=2)
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and not w.health.suspended:
        time.sleep(0.01)
    assert w.health.suspended
    n = len(calls)
    time.sleep(0.1)
    assert len(calls) == n, "a suspended worker was spinning"
    stop.set()
    t.join(timeout=10)


# ------------------------- the supervisor -------------------------
def test_one_worker_failing_does_not_stop_the_others():
    """The whole reason the supervisor exists."""
    good_calls, good = counter()
    _, bad = exploder()
    sup = Supervisor()
    sup.add(worker(good, name="forecast"))
    sup.add(worker(bad, name="arbitrage", max_consecutive_failures=2))
    sup.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and len(good_calls) < 3:
            time.sleep(0.01)
        assert len(good_calls) >= 3, "the healthy worker stopped too"
        assert sup.suspended() == ["arbitrage"]
    finally:
        sup.shutdown(timeout=5)


def test_a_disabled_worker_is_never_started():
    calls, job = counter()
    sup = Supervisor()
    sup.add(worker(job, name="off", enabled=False))
    sup.start()
    try:
        time.sleep(0.05)
        assert calls == []
        assert sup.workers == []
    finally:
        sup.shutdown(timeout=5)


def test_shutdown_stops_every_worker():
    sup = Supervisor()
    for name in ("a", "b", "c"):
        sup.add(worker(lambda: None, name=name))
    sup.start()
    time.sleep(0.05)
    sup.shutdown(timeout=5)
    assert all(not t.is_alive() for t in sup.threads.values())


def test_a_vanished_thread_is_noticed():
    # A thread that dies silently is worse than one that fails loudly: it
    # makes no noise, and the job it was doing simply stops happening.
    sup = Supervisor()
    w = sup.add(worker(lambda: None, name="ghost"))
    sup.start()
    time.sleep(0.05)
    sup.threads["ghost"].join(timeout=0)
    # Simulate the thread being gone while the supervisor still wants it.
    class Dead:
        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None
    sup.threads["ghost"] = Dead()
    assert sup.check_threads() == ["ghost"]
    sup.shutdown(timeout=1)


def test_health_reads_like_something_a_person_would_read():
    _, bad = exploder("open-meteo timed out")
    sup = Supervisor()
    good = sup.add(worker(lambda: None, name="forecast"))
    broken = sup.add(worker(bad, name="arbitrage", max_consecutive_failures=1))
    good.run_once()
    broken.run_once()
    text = sup.summary()
    assert "forecast" in text and "1 passes" in text
    assert "SUSPENDED" in text and "open-meteo timed out" in text


def test_config_reads_from_yaml_shape():
    c = WorkerConfig.from_dict("forecast", {"interval_seconds": 120,
                                            "enabled": True}, 900)
    assert c.name == "forecast" and c.interval == 120
    # A missing section must not be a crash on startup.
    assert WorkerConfig.from_dict("arb", None, 900).interval == 900


# ============ waiting on purpose, and saying so ============
def test_a_gate_that_returns_a_reason_records_it():
    """19 September: the bot ran fine, then went completely silent for fifteen
    minutes and was stopped by hand because it looked dead. It was not dead -
    the demo exchange had closed and BOTH workers were skipping every pass
    through a gate that returned False and logged nothing.

    A worker that is idle on purpose has to say so."""
    calls, job = counter()
    w = Worker(WorkerConfig(name="forecast", interval=0.01, idle_log_seconds=0.0),
               job, should_run=lambda: (False, "exchange reports trading_active=false"))
    assert w.run_once() is True
    assert calls == []
    assert w.health.skips == 1
    assert "trading_active=false" in w.health.last_skip_reason
    assert w.health.idle is True
    assert w.health.failures == 0, "waiting is not failing"


def test_a_plain_boolean_gate_still_works():
    calls, job = counter()
    w = worker(job, name="w")
    w.should_run = lambda: False
    assert w.run_once() is True
    assert calls == [] and w.health.idle is True


def test_the_summary_says_waiting_not_idle():
    # "idle" reads like "has nothing to do". "WAITING - exchange closed" reads
    # like the truth.
    _, job = counter()
    w = Worker(WorkerConfig(name="forecast", interval=0.01), job,
               should_run=lambda: (False, "exchange reports trading_active=false"))
    w.run_once()
    text = w.health.summary()
    assert "WAITING" in text
    assert "trading_active=false" in text


def test_resuming_is_announced_and_clears_the_wait():
    state = {"open": False}
    calls, job = counter()
    w = Worker(WorkerConfig(name="forecast", interval=0.01), job,
               should_run=lambda: (state["open"], "closed"))
    w.run_once()
    assert w.health.idle is True
    state["open"] = True
    w.run_once()
    assert w.health.idle is False
    assert calls == [1]


def test_a_supervisor_knows_when_everything_is_merely_waiting():
    # All waiting is NOT all suspended: the bot should keep running, because
    # the exchange will open again.
    sup = Supervisor()
    for name in ("forecast", "arbitrage"):
        sup.add(Worker(WorkerConfig(name=name, interval=0.01),
                       lambda: None, should_run=lambda: (False, "closed")))
    for w in sup.workers:
        w.run_once()
    assert sup.waiting() == ["forecast", "arbitrage"]
    assert sup.suspended() == []
    assert sup.all_waiting() is True
    assert not all(w.health.suspended for w in sup.workers), (
        "a waiting bot must not be mistaken for a dead one and shut down")
