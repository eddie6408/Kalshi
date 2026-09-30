"""Package 14A phase 4: is the outside data good enough to trade on?

Four checks, run on a forecast estimate before it is priced:

    DATA_CONFLICT       ensemble mean and the NWS forecast disagree
    SOURCE_UNAVAILABLE  a source the strategy requires is DOWN
    STALE_SOURCE        the forecast or the live observation is too old
    OBSERVATION_QC      the live "high so far" reading looks like a sensor fault

Each has a mode:

    enforce   the market is skipped, with the reason
    shadow    nothing is skipped; the verdict is recorded as "would reject"
    off       not evaluated

Only ONE thing is enforced from the start: a disagreement past
`conflict_hard_cap_f` (5F). A gap that size is a broken or stale source, not
an opinion, and it never fires in normal conditions. Everything else starts
in shadow, and is switched to enforce only after the operator has seen what
it would have blocked (scripts/fit_conflict_threshold.py for the fitted
conflict threshold).

Never "news -> order": external data reaches a trade only through this and
then the risk gate, never around them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .sources import DOWN, SourceMonitor

MODES = ("enforce", "shadow", "off")

#: NWS observation quality-control flags that mean "do not trust this value".
#: X = failed a check (rejected), Q = questioned. V/C/S/Z are usable.
BAD_QC_FLAGS = {"X", "Q"}


@dataclass
class GateConfig:
    conflict_hard_cap_f: float = 5.0
    conflict_threshold_f: float | None = None      # fitted; None = not set yet
    #: Per-kind fitted thresholds (daily HIGH / LOW markets). Each falls back
    #: to conflict_threshold_f when unset. Lows get their own because late in
    #: the day the NWS gridpoint "low" covers only the remaining hours.
    conflict_threshold_high_f: float | None = None
    conflict_threshold_low_f: float | None = None
    conflict_mode: str = "shadow"                  # applies to the fitted threshold
    source_down_mode: str = "shadow"
    stale_mode: str = "shadow"
    max_forecast_age_seconds: float = 129600.0
    max_observation_age_seconds: float = 7200.0
    obs_qc_mode: str = "shadow"

    @classmethod
    def from_dict(cls, d: dict | None) -> "GateConfig":
        d = d or {}

        def mode(key, default):
            v = str(d.get(key, default)).lower()
            if v not in MODES:
                raise ValueError(f"sources.gates.{key} must be one of {MODES}, got {v!r}")
            return v
        thr = d.get("conflict_threshold_f")

        def opt(key):
            v = d.get(key)
            return float(v) if v not in (None, "") else None
        return cls(
            conflict_threshold_high_f=opt("conflict_threshold_high_f"),
            conflict_threshold_low_f=opt("conflict_threshold_low_f"),
            conflict_hard_cap_f=float(d.get("conflict_hard_cap_f", 5.0)),
            conflict_threshold_f=float(thr) if thr not in (None, "") else None,
            conflict_mode=mode("conflict_mode", "shadow"),
            source_down_mode=mode("source_down_mode", "shadow"),
            stale_mode=mode("stale_mode", "shadow"),
            max_forecast_age_seconds=float(d.get("max_forecast_age_seconds", 129600)),
            max_observation_age_seconds=float(d.get("max_observation_age_seconds", 7200)),
            obs_qc_mode=mode("obs_qc_mode", "shadow"),
        )


@dataclass
class Verdict:
    gate: str
    enforced: bool
    detail: str

    @property
    def label(self) -> str:
        return self.gate if self.enforced else f"WOULD_{self.gate}"


@dataclass
class GateResult:
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def blocked(self) -> Verdict | None:
        return next((v for v in self.verdicts if v.enforced), None)


def evaluate(evidence: dict, cfg: GateConfig, monitor: SourceMonitor | None = None,
             strategy: str = "") -> GateResult:
    """Run every gate that is not off. Pure: records nothing itself."""
    ev = evidence or {}
    out = GateResult()

    def fire(gate: str, mode: str, detail: str) -> None:
        if mode != "off":
            out.verdicts.append(Verdict(gate, mode == "enforce", detail))

    # ---- DATA_CONFLICT -------------------------------------------------
    diff = ev.get("cross_check_diff_f")
    if diff is not None:
        gap = abs(float(diff))
        what = (f"ensemble {ev.get('ensemble_mean_f')}F vs "
                f"{ev.get('cross_check_source') or 'cross-check'} "
                f"{ev.get('cross_check_f')}F ({float(diff):+.1f}F)")
        if gap > cfg.conflict_hard_cap_f:
            fire("DATA_CONFLICT", "enforce",
                 f"{what} - past the {cfg.conflict_hard_cap_f:g}F hard cap")
        else:
            kind = str(ev.get("kind") or "").lower()
            thr = {"high": cfg.conflict_threshold_high_f,
                   "low": cfg.conflict_threshold_low_f}.get(kind)
            if thr is None:
                thr = cfg.conflict_threshold_f
            if thr is not None and gap > thr:
                fire("DATA_CONFLICT", cfg.conflict_mode,
                     f"{what} - past the {thr:g}F fitted threshold"
                     f"{' for daily ' + kind + 's' if kind in ('high', 'low') else ''}")

    # ---- SOURCE_UNAVAILABLE --------------------------------------------
    if monitor is not None and strategy:
        down = [s.id for s in monitor.registry.required_by(strategy)
                if monitor.state(s.id) == DOWN]
        if down:
            fire("SOURCE_UNAVAILABLE", cfg.source_down_mode,
                 f"required source(s) DOWN: {', '.join(down)}")

    # ---- STALE_SOURCE --------------------------------------------------
    f_age = ev.get("forecast_age_seconds")
    if f_age is not None and f_age > cfg.max_forecast_age_seconds:
        fire("STALE_SOURCE", cfg.stale_mode,
             f"forecast published {f_age / 3600:.1f}h ago, limit "
             f"{cfg.max_forecast_age_seconds / 3600:.0f}h")
    o_age = ev.get("observation_age_seconds")
    if (ev.get("observed_so_far_f") is not None and o_age is not None
            and o_age > cfg.max_observation_age_seconds):
        fire("STALE_SOURCE", cfg.stale_mode,
             f"newest station observation is {o_age / 60:.0f} min old, limit "
             f"{cfg.max_observation_age_seconds / 60:.0f} min")

    # ---- OBSERVATION_QC ------------------------------------------------
    if ev.get("observed_so_far_f") is not None and ev.get("observation_suspect"):
        fire("OBSERVATION_QC", cfg.obs_qc_mode,
             f"observed {ev.get('observed_so_far_f')}F is suspect: "
             f"{ev.get('observation_suspect')}")

    return out
