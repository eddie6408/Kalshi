"""Resource / health monitoring so this bot never destabilizes the other application on the host."""
from __future__ import annotations

import os
import shutil
import time
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


class HealthMonitor:
    def __init__(self, health_cfg, db, data_dir):
        self.cfg = health_cfg
        self.db = db
        self.data_dir = data_dir
        self.proc = psutil.Process(os.getpid()) if psutil else None
        if self.proc:
            self.proc.cpu_percent(None)

    def sample(self, extra: dict[str, Any]) -> dict[str, Any]:
        rss = self.proc.memory_info().rss / 1e6 if self.proc else 0.0
        cpu = self.proc.cpu_percent(None) if self.proc else 0.0
        sysmem = psutil.virtual_memory().percent if psutil else 0.0
        disk_free = shutil.disk_usage(self.data_dir).free / 1e6
        db_mb = self.db.size_mb()
        row = {"ts": time.time(), "cpu_pct": cpu, "rss_mb": round(rss, 1), "sys_mem_pct": sysmem,
               "disk_free_mb": round(disk_free, 1), "db_mb": round(db_mb, 1),
               "req_per_sec": extra.get("req_per_sec"), "rate_limited": extra.get("rate_limited"),
               "ws_connected": extra.get("ws_connected"), "data_connected": extra.get("data_connected"),
               "loop_lag_ms": extra.get("loop_lag_ms"), "open_positions": extra.get("open_positions"),
               "details": {k: v for k, v in extra.items() if k not in {
                   "req_per_sec", "rate_limited", "ws_connected", "data_connected", "loop_lag_ms", "open_positions"}}}
        self.db.insert("system_health", row)
        return row

    def pressure(self, row: dict[str, Any]) -> list[str]:
        why = []
        if row["rss_mb"] > self.cfg.max_rss_mb:
            why.append(f"RSS {row['rss_mb']}MB > {self.cfg.max_rss_mb}MB")
        if row["db_mb"] > self.cfg.max_db_mb:
            why.append(f"DB {row['db_mb']}MB > {self.cfg.max_db_mb}MB")
        if row["disk_free_mb"] < self.cfg.min_free_disk_mb:
            why.append(f"free disk {row['disk_free_mb']}MB < {self.cfg.min_free_disk_mb}MB")
        return why
