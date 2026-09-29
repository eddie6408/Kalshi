"""WX-1: read a Kalshi weather market's rules and say exactly what it is.

Pure functions, no network. Input is the market as the public API returns it
(rules_primary, rules_secondary, strike fields, times) plus its series; output
is a `Contract`: class, variable, aggregation, operator, thresholds, window,
station, settlement source, rounding and data rules - or the reason it could
not be read.

The settlement source is taken from the market's own rules text, never
assumed per class. Verified 2026-09-29 against the live exchange, the sources
really do differ by market:

    daily high / low   "... (CLINYC) ... according to The Weather Company"
                        (the NWS climate report value, published by TWC)
    hourly (KXTEMP*HS) "... as reported by Synoptic Data" - the Kalshi Weather
                        Index, an equal-weight mean of 5-8 ASOS stations
    hourly (Apr 2026)  "... as reported by Accuweather (for coordinates ...)"
    weekly run-length  mean of TWC HOURLY values, rounded to a whole degree
    monthly KXSJCLOWT  any single TWC hourly value
    HDD (KXHDDCVG)     TWC's published HDD(65) per day, summed

A market whose source cannot be read is RESEARCH_ONLY with the reason, and is
never tradable.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

PARSER_VERSION = "wx-taxonomy@1"

CLASSES = ("DAILY_HIGH", "DAILY_LOW", "DAILY_AVG", "HOURLY_TEMP", "PRECIPITATION",
           "WEEKLY_MAX", "WEEKLY_MIN", "MONTHLY_MAX", "MONTHLY_MIN", "RUN_LENGTH",
           "SEASONAL", "CLIMATE_ONE_OFF", "OTHER")

STATUSES = ("DISCOVERED", "CLASSIFIED", "RESEARCH_ONLY", "MODEL_READY", "PAPER_TRADING",
            "PRODUCTION_ELIGIBLE", "DISABLED")

#: Classes the extended engine (WX-4) and hourly model (WX-5) can price.
MODELLED = {"RUN_LENGTH", "WEEKLY_MAX", "WEEKLY_MIN", "MONTHLY_MAX", "MONTHLY_MIN",
            "SEASONAL", "HOURLY_TEMP"}

# Settlement sources, most specific first. The first one NAMED AS THE SOURCE
# in the rules wins; secondary-text mentions of "not authoritative" sources
# are ignored.
_SOURCES = (
    ("SYNOPTIC_KALSHI_INDEX", r"synoptic data|kalshi weather index"),
    ("ACCUWEATHER", r"accuweather"),
    ("TWC", r"the weather company|weather\.com/kalshi|the weather channel"),
    ("NWS_CLI", r"national weather service|nws climatological|climatological (?:report|records)"
                r"|source agency's (?:daily or monthly )?climatological"),
    ("NOAA_NHC", r"national hurricane center"),
    ("NOAA", r"\bnoaa\b|national oceanic|ncei|national centers for environmental"),
    ("NASA", r"\bnasa\b|goddard"),
    ("USGS", r"\busgs\b|geological survey"),
    ("NSIDC", r"snow (?:&|and) ice data center"),
)
_NOT_AUTHORITATIVE = re.compile(
    r"not authoritative|may (?:be useful|help|serve)|not any other|such as|guide your|"
    r"alternative sources|references?,? but", re.I)

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_DATE = r"([A-Z][a-z]{2,8})\.? (\d{1,2}),? (\d{4})"


@dataclass
class Contract:
    ticker: str
    event_ticker: str = ""
    series: str = ""
    title: str = ""
    cls: str = "OTHER"
    variable: str = ""            # temp_max temp_min temp_mean temp_mean_hourly temp_hourly
    #                               temp_index precip hdd other
    aggregation: str = ""         # DAILY HOURLY_AT WINDOW_MAX WINDOW_MIN ANY_HOURLY
    #                               RUN_LENGTH CUMULATIVE SPAN_COUNT
    operator: str = ""            # > >= < <= between  (on the weather variable)
    threshold: float | None = None
    threshold_hi: float | None = None
    run_k: int | None = None      # RUN_LENGTH: at least k consecutive days
    unit: str = "F"
    window_start: str | None = None
    window_end: str | None = None
    at_time: str | None = None    # HOURLY_TEMP: "7 AM EDT"
    station: str = ""             # ICAO (KPHX) where the rules give one
    cli: str = ""                 # NWS climate report code (NYC for CLINYC)
    location: str = ""
    source: str = ""
    source_text: str = ""
    rounding: str = ""
    min_hours: int | None = None
    revision_policy: str = ""
    strike_type: str = ""
    floor_strike: float | None = None
    cap_strike: float | None = None
    close_time: str = ""
    parse_ok: bool = True
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def window_days(self) -> int | None:
        if not self.window_start or not self.window_end:
            return None
        return (date.fromisoformat(self.window_end) - date.fromisoformat(self.window_start)).days + 1


def rules_hash(primary: str, secondary: str) -> str:
    return hashlib.sha256(((primary or "") + "\n\x00\n" + (secondary or "")).encode()).hexdigest()


def _d(mon: str, day: str, year: str) -> str | None:
    m = MONTHS.get(mon[:3].lower())
    if not m:
        return None
    try:
        return date(int(year), m, int(day)).isoformat()
    except ValueError:
        return None


def parse_window(text: str, open_time: str | None = None) -> tuple[str | None, str | None]:
    """(start, end) ISO dates of the period the rules measure, inclusive."""
    t = text or ""
    m = re.search(rf"(?:within|between|from)\s+{_DATE}\s+(?:through|and|to|-|–)\s+{_DATE}", t)
    if m:
        return _d(*m.group(1, 2, 3)), _d(*m.group(4, 5, 6))
    # "in September 28–October 4, 2026"
    m = re.search(r"\bin\s+([A-Z][a-z]{2,8})\.? (\d{1,2})\s*[–-]\s*([A-Z][a-z]{2,8})\.? (\d{1,2}),? (\d{4})", t)
    if m:
        y = m.group(5)
        s, e = _d(m.group(1), m.group(2), y), _d(m.group(3), m.group(4), y)
        if s and e and e < s:           # Dec 28 - Jan 3
            s = _d(m.group(1), m.group(2), str(int(y) - 1))
        return s, e
    # "before Jan 1, 2027": from issuance to the day before
    m = re.search(rf"before\s+{_DATE}", t)
    if m and re.search(r"after issuance|span of \d+ consecutive", t, re.I):
        end = _d(*m.group(1, 2, 3))
        if end:
            end = (date.fromisoformat(end) - timedelta(days=1)).isoformat()
        start = (open_time or "")[:10] or None
        return start, end
    m = re.search(rf"(?:for|on|in)\s+{_DATE}", t)
    if m:
        d = _d(*m.group(1, 2, 3))
        return d, d
    # "in Sep 2026"
    m = re.search(r"\bin\s+([A-Z][a-z]{2,8})\.? (\d{4})\b", t)
    if m and MONTHS.get(m.group(1)[:3].lower()):
        y, mo = int(m.group(2)), MONTHS[m.group(1)[:3].lower()]
        nxt = date(y + (mo == 12), mo % 12 + 1, 1)
        return date(y, mo, 1).isoformat(), (nxt - timedelta(days=1)).isoformat()
    return None, None


def parse_source(primary: str, secondary: str, series_sources: list | None = None
                 ) -> tuple[str, str, list]:
    """(source code, the phrase it came from, ambiguity notes)."""
    notes = []

    def find(text):
        hits = []
        for code, pat in _SOURCES:
            m = re.search(pat, text or "", re.I)
            if m:
                hits.append((m.start(), code, m.group(0)))
        return [h[1:] for h in sorted(hits)]

    # 1. The primary rule names it: "according to X" / "as reported by X".
    m = re.search(r"(?:according to|as reported by|reported by|published by|documented by|"
                  r"recorded by|as confirmed by|records?)\s+(.{3,90})", primary or "", re.I)
    if m:
        hits = find(m.group(1))
        if hits:
            return hits[0][0], hits[0][1], notes
    hits = find(primary)
    if hits:
        return hits[0][0], hits[0][1], notes
    # 2. The secondary text, skipping sentences that name OTHER sources as
    #    references ("... may be useful references, but are not authoritative").
    cand = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", secondary or ""):
        if _NOT_AUTHORITATIVE.search(sent):
            continue
        for code, phrase in find(sent):
            if code not in [c for c, _ in cand]:
                cand.append((code, phrase))
    if len(cand) == 1:
        return cand[0][0], cand[0][1], notes
    if len(cand) > 1:
        notes.append("rules name several possible sources: " + ", ".join(c for c, _ in cand))
        return cand[0][0], cand[0][1], notes
    # 3. The series metadata.
    for s in series_sources or []:
        hits = find((s or {}).get("name", ""))
        if hits:
            notes.append("source taken from series metadata, not the market rules")
            return hits[0][0], (s or {}).get("name", ""), notes
    notes.append("no settlement source found in the rules")
    return "", "", notes


def _op(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"strictly greater than|greater than|above|more than|exceeds?", t):
        return ">"
    if re.search(r"strictly less than|less than|below|fewer than", t):
        return "<"
    if re.search(r"at least|or (?:more|higher|above)", t):
        return ">="
    if re.search(r"at most|or (?:less|lower|below)", t):
        return "<="
    if "between" in t:
        return "between"
    return ""


def _num(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def classify(market: dict, series: dict | None = None) -> Contract:
    """Everything we can say about one market from its own rules."""
    series = series or {}
    p = market.get("rules_primary") or ""
    s = market.get("rules_secondary") or ""
    both = f"{p}\n{s}"
    low = both.lower()
    c = Contract(
        ticker=market.get("ticker", ""), event_ticker=market.get("event_ticker", ""),
        series=(series.get("ticker") or market.get("series_ticker") or
                (market.get("event_ticker") or "").split("-")[0]),
        title=market.get("title") or series.get("title") or "",
        strike_type=market.get("strike_type") or "",
        floor_strike=_num(market.get("floor_strike")), cap_strike=_num(market.get("cap_strike")),
        close_time=market.get("close_time") or "")
    c.source, c.source_text, notes = parse_source(p, s, series.get("settlement_sources"))
    c.notes.extend(notes)
    c.window_start, c.window_end = parse_window(p, market.get("open_time"))
    if "later revisions are disregarded" in low or "as published at the expiration" in low:
        c.revision_policy = "as_published_at_expiration"
    elif "corrected value shall be used" in low or "corrects a documented value" in low:
        c.revision_policy = "corrections_before_expiration"
    elif "material error" in low:
        c.revision_policy = "first_non_preliminary_unless_material_error"
    m = re.search(r"\((CLI([A-Z]{3,4}))\)|\bat (CLI([A-Z]{3,4}))\b", p)
    if m:
        c.cli = m.group(2) or m.group(4)
    m = re.search(r"\b(K[A-Z]{3})\b", p) or re.search(r"station identifier (K[A-Z]{3})", s)
    if m:
        c.station = m.group(1)
    m = re.search(r"recorded (?:at|for) (.+?)(?: \(CLI| for | as reported| at \d)", p) or \
        re.search(r"at K[A-Z]{3} in (.+?) (?:is|within)", p)
    if m:
        c.location = m.group(1).strip(" ,")

    temperature = bool(re.search(r"temperature|degree days|\bhdd\b|\bcdd\b|hottest|coldest", low))
    wdays = c.window_days

    if not temperature:
        if re.search(r"precipitation|rainfall|snowfall|inches of (?:rain|snow)", p, re.I):
            c.cls, c.variable, c.aggregation = "PRECIPITATION", "precip", "CUMULATIVE"
            c.operator, c.threshold, c.unit = _op(p), c.floor_strike, "in"
        else:
            c.cls, c.variable = "OTHER", "other"
        return _finish(c)

    # ---- temperature ------------------------------------------------------
    if re.search(r"land.ocean|global mean|anomaly|hottest .* ever|pre-industrial", low):
        c.cls, c.variable = "CLIMATE_ONE_OFF", "temp_global"
        return _finish(c)
    if re.search(r"span of \d+ consecutive calendar days", low):
        c.cls, c.variable, c.aggregation = "CLIMATE_ONE_OFF", "temp_min", "SPAN_COUNT"
        m = re.search(r"below (-?\d+(?:\.\d+)?)", p)
        c.operator, c.threshold = "<", _num(m.group(1)) if m else None
        return _finish(c)
    if re.search(r"heating degree days|cooling degree days|\bhdd\b|\bcdd\b", low):
        c.variable = "hdd" if re.search(r"heating degree|\bhdd\b", low) else "cdd"
        c.aggregation, c.unit = "CUMULATIVE", "degree_days"
        c.cls = "SEASONAL"            # degree-day totals are season-scale by construction
        c.operator = {"less": "<", "greater": ">", "less_or_equal": "<=",
                      "greater_or_equal": ">="}.get(c.strike_type, _op(p))
        c.threshold = c.cap_strike if c.operator in ("<", "<=") else c.floor_strike
        m = re.search(r"65 minus|below 65|HDD \(65\)", s)
        if m:
            c.rounding = "daily HDD(65) as published"
        return _finish(c)
    if "consecutive days" in low and "average temperature" in low:
        c.cls, c.aggregation = "RUN_LENGTH", "RUN_LENGTH"
        c.variable = "temp_mean_hourly" if "hourly temperature values" in low else "temp_mean"
        m = re.search(r"at least (\d+)", p)
        c.run_k = int(m.group(1)) if m else (int(c.floor_strike) if c.floor_strike else None)
        m = re.search(r"is (strictly )?(greater|less) than (-?\d+(?:\.\d+)?)", p)
        if m:
            c.operator = ">" if m.group(2) == "greater" else "<"
            c.threshold = float(m.group(3))
        if "nearest whole degree" in low:
            c.rounding = "nearest_whole_degree"
        m = re.search(r"fewer than (\d+) reported hourly values", low)
        c.min_hours = int(m.group(1)) if m else None
        return _finish(c)
    if re.search(r"any (?:single )?hourly temperature value", low):
        c.variable, c.aggregation = "temp_hourly", "ANY_HOURLY"
        m = re.search(r"is (strictly )?(less|greater) than (-?\d+(?:\.\d+)?)", p)
        if m:
            c.operator = "<" if m.group(2) == "less" else ">"
            c.threshold = float(m.group(3))
        kind = "MIN" if c.operator in ("<", "<=") else "MAX"
        c.cls = (f"WEEKLY_{kind}" if (wdays or 0) <= 10 else
                 f"MONTHLY_{kind}" if (wdays or 0) <= 35 else "SEASONAL")
        return _finish(c)
    m = re.search(r"\b(\d{1,2}(?::\d{2})? ?[AP]M(?: [A-Z]{2,4}T)?)\b", p)
    if re.search(r"the temperature (?:recorded|reported|value)", low) and m:
        c.cls, c.aggregation, c.at_time = "HOURLY_TEMP", "HOURLY_AT", m.group(1)
        c.variable = "temp_index" if c.source == "SYNOPTIC_KALSHI_INDEX" else "temp_hourly"
        c.operator = {"greater": ">", "less": "<", "between": "between",
                      "greater_or_equal": ">=", "less_or_equal": "<="}.get(c.strike_type, _op(p))
        c.threshold = c.floor_strike if c.operator != "<" else c.cap_strike
        c.threshold_hi = c.cap_strike if c.operator == "between" else None
        m2 = re.search(r"coordinates (-?\d+\.\d+),\s*(-?\d+\.\d+)", p)
        if m2:
            c.notes.append(f"coordinates {m2.group(1)},{m2.group(2)}")
        return _finish(c)
    # The PRIMARY rule decides: the boilerplate secondary text says
    # "maximum/minimum temperature" on every daily market.
    pl = p.lower()
    kind = ("MAX" if re.search(r"maximum|highest|high temperature", pl) else
            "MIN" if re.search(r"minimum|lowest|low temperature", pl) else
            "AVG" if "average" in pl else "")
    c.operator = _op(re.sub(r"according to.*", "", p))
    if c.strike_type == "between":
        c.operator = "between"
    c.threshold = c.floor_strike if c.floor_strike is not None else c.cap_strike
    c.threshold_hi = c.cap_strike if c.operator == "between" else None
    if c.threshold is None:
        m = re.search(r"(?:than|above|below) (-?\d+(?:\.\d+)?)°?", p)
        c.threshold = _num(m.group(1)) if m else None
    c.rounding = c.rounding or "integer_report"
    if wdays == 1:
        c.cls = {"MAX": "DAILY_HIGH", "MIN": "DAILY_LOW", "AVG": "DAILY_AVG"}.get(kind, "OTHER")
        c.variable = {"MAX": "temp_max", "MIN": "temp_min", "AVG": "temp_mean"}.get(kind, "")
        c.aggregation = "DAILY"
    elif wdays and kind in ("MAX", "MIN"):
        c.cls = (f"WEEKLY_{kind}" if wdays <= 10 else f"MONTHLY_{kind}" if wdays <= 35
                 else "SEASONAL")
        c.variable = "temp_max" if kind == "MAX" else "temp_min"
        c.aggregation = "WINDOW_MAX" if kind == "MAX" else "WINDOW_MIN"
    else:
        c.cls = "OTHER" if not kind else "SEASONAL"
        c.parse_ok = False
        c.notes.append("temperature market whose period or aggregation could not be read")
    return _finish(c)


def _finish(c: Contract) -> Contract:
    if not c.source:
        c.parse_ok = False
    if c.cls in MODELLED and c.cls != "HOURLY_TEMP":
        if not c.window_start or not c.window_end:
            c.parse_ok = False
            c.notes.append("no measurement window")
        if c.threshold is None:
            c.parse_ok = False
            c.notes.append("no threshold")
    return c


def initial_status(c: Contract, live_series: dict | None = None) -> tuple[str, str]:
    """(status, reason) for a newly classified market.

    live_series: {series prefix: expected CLI code} for the daily high/low
    series the strategy already trades on demo. Those stay PAPER_TRADING -
    unless the rules now name a different station, which DISABLES them.
    """
    live_series = live_series or {}
    if c.cls == "OTHER":
        return "DISABLED", "outside the weather-temperature scope"
    if not c.parse_ok:
        return "RESEARCH_ONLY", "rules not fully parsed: " + "; ".join(c.notes or ["unknown"])
    if c.cls in ("DAILY_HIGH", "DAILY_LOW"):
        want = None
        for prefix, cli in live_series.items():
            if c.series.upper().startswith(prefix.upper()):
                want = cli
                break
        if want is None:
            return "CLASSIFIED", "daily series not configured for the live strategy"
        if want and c.cli and want.upper() != c.cli.upper():
            return "DISABLED", (f"rules name station CLI{c.cli}, the strategy prices "
                                f"CLI{want} - settlement mismatch")
        if c.source not in ("TWC", "NWS_CLI"):
            return "DISABLED", f"daily market settles on {c.source}, not the NWS climate report"
        return "PAPER_TRADING", "live daily strategy (demo orders, production-price paper)"
    if c.cls in MODELLED:
        return "RESEARCH_ONLY", "model produces probabilities; never traded until validated"
    return "RESEARCH_ONLY", "no model for this class yet"


def parse_local_hour(at_time: str) -> tuple[int, int] | None:
    """'7 AM EDT' -> (7, 0); '12 PM' -> (12, 0)."""
    m = re.match(r"(\d{1,2})(?::(\d{2}))? ?([AP])M", at_time or "")
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "P" else 0)
    return h, int(m.group(2) or 0)


def parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
