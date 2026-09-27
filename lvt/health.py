"""Resource monitoring (section 56): CPU, RAM, disk, database size, request
rate, 429s. The service also runs under systemd CPU/memory caps so it
cannot starve the other bot on the droplet."""
from __future__ import annotations

import os
import resource
import shutil
import time
from pathlib import Path


class ResourceMonitor:
    def __init__(self):
        self._last_cpu = self._cpu()
        self._last_t = time.monotonic()

    @staticmethod
    def _cpu() -> float:
        r = resource.getrusage(resource.RUSAGE_SELF)
        return r.ru_utime + r.ru_stime

    def sample(self, data_dir: Path, state_db: Path, tape_db: Path) -> dict:
        cpu, t = self._cpu(), time.monotonic()
        pct = 100.0 * (cpu - self._last_cpu) / max(t - self._last_t, 1e-6)
        self._last_cpu, self._last_t = cpu, t
        return {
            "cpu_pct": round(pct, 1),
            "rss_mb": round(rss_mb(), 1),
            "disk_free_gb": round(shutil.disk_usage(data_dir).free / 1e9, 2),
            "state_db_mb": round(db_size_mb(state_db), 1),
            "tape_db_mb": round(db_size_mb(tape_db), 1),
        }


def rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024.0 * 1024.0) if os.uname().sysname == "Darwin" else r / 1024.0


def db_size_mb(p: Path) -> float:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(p) + suffix)
        if q.exists():
            total += q.stat().st_size
    return total / 1e6
