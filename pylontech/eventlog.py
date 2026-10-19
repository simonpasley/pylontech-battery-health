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
