"""Parse and summarise Pylontech `log` (event-log) dumps into plain English.

The raw `log` command returns a stack of entries like:

    Index   : 31
    Time    : 26-10-19 23:29:40
    ModID   : BMM
    Code    : 72d
    Info    : The voltage of device is too low.

On its own that's a wall of text an installer still has to interpret. This
module turns it into a structured, classified, plain-English summary:

  * every entry parsed into a LogEvent (index, timestamp, code, info)
  * each entry classified (under-voltage trip, self-restart, normal cycling…)
    with a severity so the important lines stand out from routine noise
  * a one-paragraph verdict: did this pack just log protection faults, or is
    it only normal charge/discharge activity?

Designed to be read by a non-expert. The goal is that the headline answers
"should I worry about this pack?" before the user reads a single raw line.

Tested against US3000C / US5000 B69-series `log` output.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# Severity ranks, low → high. Used to sort the headline and to colour the UI.
INFO = "info"          # routine: charge/discharge cycling, SOC low/normal
NOTABLE = "notable"    # worth noting: self-restart, shutdown
CRITICAL = "critical"  # protection trip: a real fault signature


# Classification rules, checked in order. Each entry's Info string is lower-
# cased and matched against ALL keywords in the tuple (AND). First match wins.
# Keyword matching (not exact codes) survives firmware wording differences.
_RULES = [
    (("voltage", "too low"),      "undervoltage",  "Under-voltage protection trip", CRITICAL),
    (("voltage", "too high"),     "overvoltage",   "Over-voltage protection trip",  CRITICAL),
    (("over current",),           "overcurrent",   "Over-current protection trip",  CRITICAL),
    (("short circuit",),          "shortcircuit",  "Short-circuit protection trip", CRITICAL),
    (("temperature", "too high"), "overtemp",      "Over-temperature trip",         CRITICAL),
    (("temperature", "too low"),  "undertemp",     "Under-temperature trip",        CRITICAL),
    (("system is restarting",),   "restart",       "BMS self-restart",              NOTABLE),
    (("shut down",),              "shutdown",      "Pack shut down",                NOTABLE),
    (("shutdown",),               "shutdown",      "Pack shut down",                NOTABLE),
    (("coulomb", "low"),          "soc_low",       "State-of-charge low",           INFO),
    (("coulomb", "normal"),       "soc_normal",    "State-of-charge back to normal", INFO),
    (("start discharging",),      "discharge",     "Started discharging",           INFO),
    (("start charging",),         "charge",        "Started charging",              INFO),
]


@dataclass
class LogEvent:
    index: int
    time_raw: str
    timestamp: Optional[datetime]
    mod_id: str
    code: str
    info: str
    category: str   # machine key, e.g. 'undervoltage'
    label: str      # human label, e.g. 'Under-voltage protection trip'
    severity: str   # INFO / NOTABLE / CRITICAL


@dataclass
class EventLogSummary:
    events: list[LogEvent] = field(default_factory=list)
    span_start: Optional[datetime] = None
    span_end: Optional[datetime] = None
    counts: dict = field(default_factory=dict)        # category -> count
    critical: list[LogEvent] = field(default_factory=list)
    notable: list[LogEvent] = field(default_factory=list)
    flapping: bool = False          # rapid charge/discharge toggling detected
    headline: str = ""              # one-line plain-English verdict
    severity: str = INFO            # worst severity seen
    details: list[str] = field(default_factory=list)  # plain-English bullet points


def _classify(info: str) -> tuple[str, str, str]:
    low = info.lower()
    for keywords, category, label, severity in _RULES:
        if all(k in low for k in keywords):
            return category, label, severity
    return "other", info.rstrip(". ").strip() or "Unclassified event", INFO


def _parse_time(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    for fmt in ("%y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_event_log(text: str) -> list[LogEvent]:
    """Parse raw `log` dump text into structured LogEvents.

    Robust to the dump's header comments, the paging prompts
    ("Press [Enter] to be continued…"), banner lines and blank padding —
    it only consumes the five known field lines per entry.
    """
    events: list[LogEvent] = []
    cur: dict = {}

    def _flush():
        if "index" not in cur:
            return
        info = cur.get("info", "")
        category, label, severity = _classify(info)
        events.append(LogEvent(
            index=cur["index"],
            time_raw=cur.get("time", ""),
            timestamp=_parse_time(cur.get("time", "")),
            mod_id=cur.get("modid", ""),
            code=cur.get("code", ""),
            info=info,
            category=category,
            label=label,
            severity=severity,
        ))

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^Index\s*:\s*(\d+)", line)
        if m:
            _flush()
            cur = {"index": int(m.group(1))}
            continue
        if not cur:
            continue
        m = re.match(r"^Time\s*:\s*(.+)", line)
        if m:
            cur["time"] = m.group(1).strip()
            continue
        m = re.match(r"^ModID\s*:\s*(.+)", line)
        if m:
            cur["modid"] = m.group(1).strip()
            continue
        m = re.match(r"^Code\s*:\s*(.+)", line)
        if m:
            cur["code"] = m.group(1).strip()
            continue
        m = re.match(r"^Info\s*:\s*(.+)", line)
        if m:
            cur["info"] = m.group(1).strip()
            continue
    _flush()
    return events


def summarise_event_log(text: str) -> EventLogSummary:
    """Parse and summarise a `log` dump into a plain-English verdict."""
    events = parse_event_log(text)
    s = EventLogSummary(events=events)
    if not events:
        s.headline = "No readable events in this log dump."
        return s

    # Counts by category + worst severity.
    sev_rank = {INFO: 0, NOTABLE: 1, CRITICAL: 2}
    worst = INFO
    for e in events:
        s.counts[e.category] = s.counts.get(e.category, 0) + 1
        if sev_rank[e.severity] > sev_rank[worst]:
            worst = e.severity
        if e.severity == CRITICAL:
            s.critical.append(e)
        elif e.severity == NOTABLE:
            s.notable.append(e)
    s.severity = worst

    # Time span (ignore unparseable timestamps).
    stamps = sorted(e.timestamp for e in events if e.timestamp)
    if stamps:
        s.span_start, s.span_end = stamps[0], stamps[-1]

    # Flapping: many charge/discharge transitions packed into a short window
    # is itself a symptom (a weak cell repeatedly tripping thresholds).
    cycles = s.counts.get("charge", 0) + s.counts.get("discharge", 0)
    if cycles >= 8 and s.span_start and s.span_end:
        span_min = (s.span_end - s.span_start).total_seconds() / 60.0
        if span_min > 0 and cycles / span_min > 0.3:   # >~1 transition / 3 min
            s.flapping = True

    # ---- Plain-English details ----
    uv = s.counts.get("undervoltage", 0)
    ov = s.counts.get("overvoltage", 0)
    restarts = s.counts.get("restart", 0)
    shutdowns = s.counts.get("shutdown", 0)
    if uv:
        s.details.append(f"⚠ {uv}× under-voltage protection trip — a cell hit its low-voltage cutoff.")
    if ov:
        s.details.append(f"⚠ {ov}× over-voltage protection trip.")
    for key, label in (("overcurrent", "over-current"), ("shortcircuit", "short-circuit"),
                       ("overtemp", "over-temperature"), ("undertemp", "under-temperature")):
        if s.counts.get(key):
            s.details.append(f"⚠ {s.counts[key]}× {label} protection trip.")
    if restarts or shutdowns:
        parts = []
        if shutdowns:
            parts.append(f"{shutdowns}× shut down")
        if restarts:
            parts.append(f"{restarts}× restarted itself")
        s.details.append("⚠ The pack " + " and ".join(parts) + " — the BMS could not hold a stable state.")
    if s.flapping:
        s.details.append("⚠ Rapid charge/discharge toggling — consistent with a weak cell repeatedly hitting a threshold.")
    if not (uv or ov or restarts or shutdowns) and s.severity == INFO:
        s.details.append("No protection faults in this window — only normal charge/discharge activity logged.")

    # ---- Headline verdict ----
    if s.severity == CRITICAL:
        trips = uv + ov + sum(s.counts.get(k, 0) for k in
                              ("overcurrent", "shortcircuit", "overtemp", "undertemp"))
        headline = f"FAULTS LOGGED — this pack tripped protection {trips}×"
        if restarts or shutdowns:
            headline += " and power-cycled itself"
        s.headline = headline + "."
    elif s.severity == NOTABLE:
        s.headline = "Some abnormal events logged (shutdown/restart) but no protection trips in this window."
    else:
        s.headline = "Clean — only normal charge/discharge activity in this window."
    return s


def _window_str(s: EventLogSummary) -> str:
    if s.span_start and s.span_end:
        return f"{s.span_start:%Y-%m-%d %H:%M} → {s.span_end:%H:%M} ({len(s.events)} events)"
    return f"{len(s.events)} events"


def render_summary_text(s: EventLogSummary) -> str:
    """Render a short human-readable summary block (for console / report)."""
    lines = []
    icon = {CRITICAL: "🔴", NOTABLE: "🟡", INFO: "🟢"}[s.severity]
    lines.append(f"{icon}  {s.headline}")
    if s.span_start and s.span_end:
        lines.append(f"    Window: {_window_str(s)}")
    for d in s.details:
        lines.append(f"    • {d}")
    if s.critical:
        lines.append("    Key events:")
        for e in s.critical[:6]:
            ts = f"{e.timestamp:%H:%M:%S}" if e.timestamp else e.time_raw
            lines.append(f"      [{ts}] {e.label}  (code {e.code})")
    return "\n".join(lines)


def render_summary_filehead(s: EventLogSummary) -> str:
    """Render the plain-English summary as a comment block to prepend to the
    downloaded .txt dump, so whoever reads the file (e.g. an RMA reviewer)
    sees the verdict before a single raw line."""
    icon = {CRITICAL: "[FAULT]", NOTABLE: "[NOTE]", INFO: "[OK]"}[s.severity]
    lines = [
        "# === PLAIN-ENGLISH SUMMARY (auto-generated from the log below) ===",
        f"# {icon} {s.headline}",
    ]
    if s.span_start and s.span_end:
        lines.append(f"# Window: {_window_str(s)}")
    for d in s.details:
        lines.append(f"#   - {d}")
    if s.critical:
        lines.append("# Key events:")
        for e in s.critical[:8]:
            ts = f"{e.timestamp:%Y-%m-%d %H:%M:%S}" if e.timestamp else e.time_raw
            lines.append(f"#   [{ts}] {e.label} (code {e.code})")
    lines.append("#")
    return "\n".join(lines) + "\n"


def summary_to_dict(s: EventLogSummary) -> dict:
    """JSON-serialisable view of the summary for the web UI."""
    return {
        "headline": s.headline,
        "severity": s.severity,
        "window": _window_str(s) if s.events else "",
        "event_count": len(s.events),
        "details": list(s.details),
        "counts": dict(sorted(s.counts.items())),
        "critical": [
            {
                "time": (f"{e.timestamp:%Y-%m-%d %H:%M:%S}" if e.timestamp else e.time_raw),
                "label": e.label,
                "code": e.code,
            }
            for e in s.critical[:8]
        ],
    }


# =========================================================================== #
# Event HISTORY (`data event`) — per-cell voltage / over-voltage analysis
# =========================================================================== #
#
# The `data event` history stores each event WITH the 15 per-cell voltages and
# the BMS's own per-cell Volt.State, plus item-level Bat Events flags:
#   BOV = cell over-voltage protection trip   BHV = cell high-voltage warning
#   BUV = cell under-voltage protection trip  BLV = cell low-voltage warning
# This is the deepest evidence of an over-voltage spike — it shows the actual
# voltage a cell reached, not just that a threshold was crossed.

# A cell at/above this (mV) is into LFP high/over-voltage territory — used only
# for headline wording; the authoritative flags are the BMS's own states below.
CELL_HIGH_MV = 3600

# Human labels for the item-level Bat Event flags we care about.
_BAT_FLAG_LABEL = {
    "BOV": "cell over-voltage protection trip",
    "BHV": "cell high-voltage warning",
    "BLV": "cell low-voltage warning",
    "BUV": "cell under-voltage protection trip",
}


@dataclass
class HistorySummary:
    item_count: int = 0
    span_start: Optional[datetime] = None
    span_end: Optional[datetime] = None
    max_cell_mv: int = 0
    max_cell_num: Optional[int] = None
    max_cell_time: str = ""
    min_cell_mv: int = 0
    min_cell_num: Optional[int] = None
    max_pack_mv: int = 0
    bat_events: dict = field(default_factory=dict)        # flag -> count
    cell_state_counts: dict = field(default_factory=dict)  # Volt.State -> count
    severity: str = INFO
    headline: str = ""
    details: list = field(default_factory=list)


def summarise_event_history(text: str) -> HistorySummary:
    """Scan a `data event` history dump for over/under-voltage evidence."""
    h = HistorySummary()

    # Item-level: timestamps, pack voltage, Bat Events flags.
    times = []
    item_count = 0
    for block in re.split(r"-{10,}", text):
        if "Item Index" not in block:
            continue
        item_count += 1
        tm = re.search(r"Time\s*:\s*([\d\- :]+)", block)
        if tm:
            ts = _parse_time(tm.group(1).strip())
            # Some BMS items carry a reset-clock default (e.g. year 2016/2000)
            # from before the RTC was set — ignore those for the span so the
            # window reads sensibly, but still count the event itself.
            if ts and ts.year >= 2019:
                times.append(ts)
        pv = re.search(r"^\s*Voltage\s*:\s*(\d+)\s*mV", block, re.M)
        if pv:
            h.max_pack_mv = max(h.max_pack_mv, int(pv.group(1)))
        # Match only the "Bat Events" line itself — anchor to line start/end
        # (re.M) and use [ \t]* not \s* after the hex code, so a no-event
        # "Bat Events : 0x0" line can't swallow the newline and capture the
        # following "Bat Protect ENA : BOV BHV …" enable-mask (which lists
        # every *possible* protection, not events that fired). Whitelist the
        # four real battery-voltage flags as a second guard.
        for be in re.finditer(r"^\s*Bat Events\s*:\s*\S+[ \t]*(.*)$", block, re.M):
            for tok in be.group(1).split():
                if tok in ("BOV", "BHV", "BLV", "BUV"):
                    h.bat_events[tok] = h.bat_events.get(tok, 0) + 1
    h.item_count = item_count
    if times:
        h.span_start, h.span_end = min(times), max(times)

    # Per-cell rows (anywhere): idx volt curr tempr base voltstate ...
    rows = re.findall(
        r"^\s*(\d{1,2})\s+(\d{4})\s+(-?\d+)\s+(\d+)\s+(\w+)\s+(\w+)", text, re.M)
    h.min_cell_mv = 10000
    for cell, volt, _cur, _t, _base, vstate in rows:
        cell, volt = int(cell), int(volt)
        if volt > h.max_cell_mv:
            h.max_cell_mv, h.max_cell_num = volt, cell
        if volt < h.min_cell_mv:
            h.min_cell_mv, h.min_cell_num = volt, cell
        if vstate.lower() != "normal":
            h.cell_state_counts[vstate] = h.cell_state_counts.get(vstate, 0) + 1
    if not rows:
        h.min_cell_mv = 0

    # ---- severity + plain English ----
    ov = h.bat_events.get("BOV", 0)
    hv = h.bat_events.get("BHV", 0)
    uv = h.bat_events.get("BUV", 0)
    lv = h.bat_events.get("BLV", 0)

    if ov or uv:
        h.severity = CRITICAL
    elif hv or lv or h.cell_state_counts:
        h.severity = NOTABLE

    if h.max_cell_num is not None:
        tag = " — into high/over-voltage" if h.max_cell_mv >= CELL_HIGH_MV else ""
        h.details.append(
            f"Highest cell voltage seen: {h.max_cell_mv} mV (cell {h.max_cell_num}){tag}.")
    if h.min_cell_num is not None and h.min_cell_mv:
        h.details.append(
            f"Lowest cell voltage seen: {h.min_cell_mv} mV (cell {h.min_cell_num}).")
    for flag in ("BOV", "BHV", "BUV", "BLV"):
        n = h.bat_events.get(flag, 0)
        if n:
            h.details.append(f"{n}× {_BAT_FLAG_LABEL[flag]} ({flag}).")
    if h.max_pack_mv:
        h.details.append(f"Highest whole-pack voltage: {h.max_pack_mv / 1000:.2f} V.")

    if ov:
        h.headline = (f"OVER-VOLTAGE recorded — {ov} cell over-voltage trip(s); "
                      f"peak cell {h.max_cell_mv} mV.")
    elif hv:
        h.headline = (f"High-voltage events recorded — peak cell {h.max_cell_mv} mV "
                      f"(cell {h.max_cell_num}).")
    elif uv or lv:
        h.headline = (f"Under-/low-voltage dominant — peak cell only {h.max_cell_mv} mV; "
                      f"{uv} under-voltage trip(s).")
    elif h.item_count:
        h.headline = f"No voltage-protection events in {h.item_count} stored items."
    else:
        h.headline = "No readable events in this history dump."
    return h


def render_history_filehead(h: HistorySummary) -> str:
    icon = {CRITICAL: "[FAULT]", NOTABLE: "[NOTE]", INFO: "[OK]"}[h.severity]
    lines = [
        "# === PLAIN-ENGLISH SUMMARY (auto-generated from the history below) ===",
        f"# {icon} {h.headline}",
    ]
    if h.span_start and h.span_end:
        lines.append(f"# Window: {h.span_start:%Y-%m-%d %H:%M} -> "
                     f"{h.span_end:%Y-%m-%d %H:%M} ({h.item_count} stored events)")
    for d in h.details:
        lines.append(f"#   - {d}")
    lines.append("#")
    return "\n".join(lines) + "\n"


def history_summary_to_dict(h: HistorySummary) -> dict:
    return {
        "headline": h.headline,
        "severity": h.severity,
        "window": (f"{h.span_start:%Y-%m-%d %H:%M} → {h.span_end:%Y-%m-%d %H:%M} "
                   f"({h.item_count} stored events)" if h.span_start else ""),
        "event_count": h.item_count,
        "details": list(h.details),
        "max_cell_mv": h.max_cell_mv,
        "max_cell_num": h.max_cell_num,
        "bat_events": dict(sorted(h.bat_events.items())),
        # Shape-compatible with the event-log summary so the UI renders both.
        "counts": dict(sorted(h.bat_events.items())),
        "critical": [],
    }
