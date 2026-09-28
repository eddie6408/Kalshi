"""Package 3a: keep the market data the bot sees, instead of throwing it away.

Books, deltas and trades used to be consumed and discarded, so no decision
could ever be replayed and no fill model could be fitted. History only
accumulates forward: every day without this is a day of replay data lost.

    Recorder.record(source, rec)   hand a record over; never blocks the caller
    iter_ticks(root, day, source)  read a day back, in order

Layout: append-only gzip JSON lines, one file per source per UTC day,

    data/ticks/2026-09-28/demo_ws.jsonl.gz
                          demo_book.jsonl.gz
                          prod_book.jsonl.gz
                          prod_trades.jsonl.gz

kept outside SQLite so the journal stays small. Each batch is written as its
own complete gzip member (open, write, close), so a crash can lose at most the
batch in memory and never leaves a file that cannot be read back.

Every record carries `rt`, OUR receive time (unix seconds): replay has to
enforce "known at the time", and the exchange's own timestamp says when
something happened, not when we could have acted on it.

The recorder never gets in the way of trading. A full queue drops the record
and counts it; a write error or a disk running low stops RECORDING (and
alerts once), never the bot.
"""
from __future__ import annotations

import gzip
import json
import logging
import queue
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SOURCES = ("demo_ws", "demo_book", "prod_book", "prod_trades")


@dataclass
class RecorderConfig:
    enabled: bool = True
    directory: str = "data/ticks"
    retention_days: int = 90
    #: stop recording (not trading) below this much free disk
    min_free_gb: float = 5.0
    #: record every demo feed snapshot/delta (the bulk of the volume)
    demo_ws: bool = True
    flush_seconds: float = 5.0
    queue_size: int = 200_000

    @classmethod
    def from_dict(cls, d: dict | None) -> "RecorderConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            directory=str(d.get("directory", "data/ticks")),
            retention_days=int(d.get("retention_days", 90)),
            min_free_gb=float(d.get("min_free_gb", 5.0)),
            demo_ws=bool(d.get("demo_ws", True)),
            flush_seconds=float(d.get("flush_seconds", 5.0)),
            queue_size=int(d.get("queue_size", 200_000)),
        )


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def book_levels(book) -> dict:
    """A DepthBook as plain bid ladders, [[price, qty], ...] per side."""
    if book is None:
        return {"yes": [], "no": []}
    return {"yes": [[lv.price_cents, lv.contracts] for lv in (book.yes_bids or [])],
            "no": [[lv.price_cents, lv.contracts] for lv in (book.no_bids or [])]}


class Recorder:
    def __init__(self, root: str | Path, cfg: RecorderConfig | None = None,
                 clock=time.time, alert=None):
        self.cfg = cfg or RecorderConfig()
        self.root = Path(root)
        self.clock = clock
        #: called once, with a message, when recording stops for disk space
        self.alert = alert
        self._q: queue.Queue = queue.Queue(maxsize=self.cfg.queue_size)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.written = 0
        self.dropped = 0
        self.errors = 0
        self.paused_for_disk = False
        self._by_source: dict[str, int] = {}

    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.cfg.enabled and not self.paused_for_disk

    def wants(self, source: str) -> bool:
        if not self.active:
            return False
        return source != "demo_ws" or self.cfg.demo_ws

    def record(self, source: str, rec: dict) -> None:
        """Hand one record over. Never blocks and never raises."""
        if not self.wants(source):
            return
        rec.setdefault("rt", round(self.clock(), 3))
        try:
            self._q.put_nowait((source, rec))
        except queue.Full:
            self.dropped += 1

    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self.cfg.enabled or self._thread is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.flush()

    def _run(self) -> None:
        while not self._stop.wait(self.cfg.flush_seconds):
            self.flush()

    def flush(self) -> int:
        """Write whatever is queued. One gzip member per file per call."""
        batch: dict[tuple[str, str], list[str]] = {}
        n = 0
        while True:
            try:
                source, rec = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                line = json.dumps(rec, separators=(",", ":"), default=str)
            except (TypeError, ValueError):
                self.errors += 1
                continue
            batch.setdefault((day_of(rec["rt"]), source), []).append(line)
            n += 1
        if not batch:
            return 0
        if not self._disk_ok():
            self.dropped += n
            return 0
        for (day, source), lines in batch.items():
            path = self.root / day / f"{source}.jsonl.gz"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(path, "at", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                self.written += len(lines)
                self._by_source[source] = self._by_source.get(source, 0) + len(lines)
            except OSError as exc:
                self.errors += 1
                self.dropped += len(lines)
                log.warning("RECORDER  could not write %s: %s", path, exc)
        return n

    def _disk_ok(self) -> bool:
        where = self.root
        while not where.exists() and where.parent != where:
            where = where.parent        # not created yet: ask its disk
        try:
            free_gb = shutil.disk_usage(where).free / 1e9
        except OSError:
            return True
        if free_gb < self.cfg.min_free_gb:
            if not self.paused_for_disk:
                self.paused_for_disk = True
                msg = (f"only {free_gb:.1f} GB free (< {self.cfg.min_free_gb:.0f} GB): "
                       f"market-data recording stopped. Trading is unaffected.")
                log.error("RECORDER  %s", msg)
                if self.alert is not None:
                    try:
                        self.alert(msg)
                    except Exception:  # noqa: BLE001
                        pass
            return False
        return True

    # ------------------------------------------------------------------
    def prune(self, now: float | None = None) -> int:
        """Delete day folders older than retention_days. Returns how many."""
        if self.cfg.retention_days <= 0 or not self.root.exists():
            return 0
        now = self.clock() if now is None else now
        cutoff = (datetime.fromtimestamp(now, timezone.utc)
                  - timedelta(days=self.cfg.retention_days)).strftime("%Y-%m-%d")
        gone = 0
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and len(d.name) == 10 and d.name < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                gone += 1
        if gone:
            log.info("RECORDER  removed %d day(s) of ticks older than %d days",
                     gone, self.cfg.retention_days)
        return gone

    def summary(self) -> str:
        if not self.cfg.enabled:
            return "off"
        per = ", ".join(f"{k} {v}" for k, v in sorted(self._by_source.items()))
        state = " PAUSED (disk)" if self.paused_for_disk else ""
        return (f"{self.written} written ({per or 'nothing yet'}), "
                f"{self.dropped} dropped, {self.errors} errors{state}")


def iter_ticks(root: str | Path, day: str, source: str):
    """Every record of one source on one UTC day, in the order written."""
    path = Path(root) / day / f"{source}.jsonl.gz"
    if not path.exists():
        return
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        try:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
        except EOFError:
            # A member cut short by a crash mid-write: everything before it
            # is intact and has already been returned.
            log.warning("RECORDER  %s ends in a truncated batch", path)
