"""Cross-check engine.

Takes battery diagnostic results (from the direct serial console reader in
diagnose.py) and Cerbo readings (from the Modbus TCP reader in cerbo.py)
and emits a set of cross-checks that are only possible because we have
both sources at once.

The point of this module is the bit no single-source tool can do: spot
mismatches between what the BMS actually says about itself and what the
Cerbo has propagated through DVCC to the rest of the install. Each check
returns one CrossCheckResult with a severity, a short title and a 1-2
sentence detail. Optional suggestion text is included where there is
an actionable next step.

By design every check is defensive about missing inputs — if either
source is absent or partial, the relevant check returns None (or a
placeholder informational result) rather than guessing.
"""

from dataclasses import dataclass, field
from typing import Optional

from .cerbo import CerboReadings
from .diagnose import PackDiagnosis


# Severity levels, ordered low to high. The UI renders these distinctly
# (ok = green, info = neutral, warning = amber, alert = red).
SEVERITY_OK = "ok"
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ALERT = "alert"


@dataclass
class CrossCheckResult:
    """One finding from the cross-check engine."""
    id: str
    title: str
    severity: str
    detail: str
    suggestion: Optional[str] = None


@dataclass
class CrossCheckReport:
    """Aggregate output: applicable cross-checks + meta about what was usable."""
    results: list[CrossCheckResult] = field(default_factory=list)
    has_battery_data: bool = False
    has_cerbo_data: bool = False
    bms_verified_pylontech: bool = False    # True if we got direct serial reads from a Pylontech pack


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_crosschecks(
    diagnoses: Optional[list[PackDiagnosis]],
    cerbo: Optional[CerboReadings],
) -> CrossCheckReport:
    """Run the v1 cross-check catalogue.

    diagnoses: zero or more PackDiagnosis objects from the battery side.
    cerbo:     a CerboReadings, or None if no Cerbo connection is active.

    Returns a CrossCheckReport. Every check is independently optional —
    the engine reports whatever it can and silently skips what it can't.
    """
    diagnoses = diagnoses or []
    report = CrossCheckReport(
        has_battery_data=bool(diagnoses),
        has_cerbo_data=cerbo is not None,
        bms_verified_pylontech=bool(diagnoses),  # direct Pylontech BMS reads imply Pylontech
    )

    # Bail-out states first so the user gets a clear "what's missing" hint.
    if not report.has_cerbo_data and not report.has_battery_data:
        report.results.append(CrossCheckResult(
            id="no_data",
            title="No data to cross-check",
            severity=SEVERITY_INFO,
            detail="Connect a battery (serial cable) and/or a Cerbo (network) to enable diagnostics.",
        ))
        return report

    if not report.has_cerbo_data:
        report.results.append(CrossCheckResult(
            id="no_cerbo",
            title="No Cerbo connected",
            severity=SEVERITY_INFO,
            detail="Add a Cerbo (Settings → Add Cerbo) to enable cross-checks between battery state and install configuration.",
        ))
        return report

    # We have a Cerbo. Even with no battery serial connection, the DVCC
    # summary is useful on its own — and we want to tell the user clearly
    # if we can't verify the connected BMS is Pylontech.
    if not report.has_battery_data:
        cerbo_sees_battery = (
            cerbo.battery.voltage_v is not None or
            cerbo.battery.soc_percent is not None
        )
        if cerbo_sees_battery:
            report.results.append(CrossCheckResult(
                id="bms_brand_unverified",
                title="Connected BMS brand not verified",
                severity=SEVERITY_WARNING,
                detail=(
                    "The Cerbo is communicating with a BMS, but no direct battery serial "
                    "connection is active so we can't verify it's a Pylontech. "
                    "Pylontech-specific cross-checks are skipped until a direct serial "
                    "connection is added."
                ),
                suggestion="Plug a USB-RS232 console cable into the battery's master pack to enable the full cross-check set.",
            ))
        else:
            report.results.append(CrossCheckResult(
                id="cerbo_no_battery_service",
                title="Cerbo not receiving battery data",
                severity=SEVERITY_ALERT,
                detail=(
                    "Connected to the Cerbo but its battery service is empty — the Cerbo "
                    "isn't currently seeing any BMS on the CAN bus."
                ),
                suggestion="Check the CAN cable between battery and Cerbo (must be Victron Type A or B for Pylontech, not the Pylontech-bundled cable) and the master pack's DIP switches (must be 000).",
            ))
        # Still useful to emit the DVCC summary even without battery side.
        report.results.append(_check_dvcc_summary(cerbo))
        return report

    # Full path — we have both. Run all v1 checks.
    report.results.append(_check_dvcc_summary(cerbo))                          # #6

    cvl = _check_cvl_gap(cerbo)
    if cvl: report.results.append(cvl)                                          # #1

    comms = _check_bms_cerbo_comms(diagnoses, cerbo)
    if comms: report.results.append(comms)                                      # #3 (degraded)

    soc = _check_soc_parity(diagnoses, cerbo)
    if soc: report.results.append(soc)                                          # #4

    report.results.extend(_check_imbalance_attribution(diagnoses, cerbo))       # #7

    return report


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_dvcc_summary(cerbo: CerboReadings) -> CrossCheckResult:
    """#6: Plain-English summary of the DVCC config the Cerbo is enforcing."""
    parts: list[str] = []
    if cerbo.dvcc.max_charge_voltage_v is None:
        parts.append("no charge-voltage cap (BMS-native target in effect)")
    else:
        parts.append(f"charge-voltage cap {cerbo.dvcc.max_charge_voltage_v:.1f} V")
    if cerbo.dvcc.confidence == "best-effort":
        suffix = " (DVCC current-cap fields still being verified against the Victron register doc — treat as informational only)"
    else:
        suffix = ""
    return CrossCheckResult(
        id="dvcc_summary",
        title="DVCC configuration",
        severity=SEVERITY_INFO,
        detail="DVCC is configured with " + "; ".join(parts) + "." + suffix,
    )


def _check_cvl_gap(cerbo: CerboReadings) -> Optional[CrossCheckResult]:
    """#1: BMS-requested CVL vs Cerbo cap setting."""
    cap = cerbo.dvcc.max_charge_voltage_v
    bms_req = cerbo.battery.requested_cvl_v

    if cap is None and bms_req is None:
        return CrossCheckResult(
            id="cvl_gap",
            title="Charge-voltage policy",
            severity=SEVERITY_INFO,
            detail="No DVCC charge-voltage cap is configured. The BMS isn't currently requesting a specific charge voltage (typical when the pack is idle or discharging).",
        )

    if cap is None and bms_req is not None:
        return CrossCheckResult(
            id="cvl_gap",
            title="Charge-voltage policy",
            severity=SEVERITY_OK,
            detail=f"No DVCC cap configured; BMS is requesting {bms_req:.1f} V. Cells balance at the BMS-native target — best for long-term pack balance.",
        )

    if cap is not None and bms_req is None:
        return CrossCheckResult(
            id="cvl_gap",
            title="Charge-voltage policy",
            severity=SEVERITY_INFO,
            detail=f"DVCC cap is {cap:.1f} V; BMS isn't currently requesting a specific charge voltage. Cap will activate next time the BMS pushes a CVL request.",
        )

    # Both set
    gap = bms_req - cap
    if gap > 0.05:
        return CrossCheckResult(
            id="cvl_gap",
            title="DVCC cap is below BMS-requested charge voltage",
            severity=SEVERITY_WARNING,
            detail=(
                f"BMS is requesting {bms_req:.1f} V but DVCC cap is set to {cap:.1f} V — "
                f"cap is {gap:.2f} V below the BMS request. Cells will balance slower than the BMS-native design and may not fully balance at top of charge."
            ),
            suggestion="Raise or remove the DVCC charge-voltage cap if cell-balance is a concern. If running the cap deliberately for stability, this finding is informational.",
        )
    return CrossCheckResult(
        id="cvl_gap",
        title="Charge-voltage policy",
        severity=SEVERITY_OK,
        detail=f"DVCC cap ({cap:.1f} V) is at or above the BMS-requested {bms_req:.1f} V — cap not currently constraining.",
    )


def _check_bms_cerbo_comms(
    diagnoses: list[PackDiagnosis],
    cerbo: CerboReadings,
) -> Optional[CrossCheckResult]:
    """#3 (degraded): direct BMS responding but Cerbo not seeing it.

    Full controller-mismatch detection waits on a verified read of the DVCC
    controller-selection register. v1 catches the most common failure:
    BMS alive on serial, Cerbo battery service empty → CAN comms broken.
    """
    if not diagnoses:
        return None
    cerbo_sees_battery = (
        cerbo.battery.voltage_v is not None or
        cerbo.battery.soc_percent is not None
    )
    if cerbo_sees_battery:
        return None  # No issue; positive path covered by other checks
    return CrossCheckResult(
        id="bms_cerbo_comms",
        title="Cerbo not receiving battery data over CAN",
        severity=SEVERITY_ALERT,
        detail=(
            "The battery's BMS responds to direct serial commands, but the Cerbo's battery "
            "service is empty — CAN comms between the BMS and the Cerbo are likely broken."
        ),
        suggestion="Check the CAN cable to the Cerbo (must be Victron Type A or B for Pylontech, not the Pylontech-bundled cable). Verify the master pack's DIP switches are set to 000.",
    )


def _check_soc_parity(
    diagnoses: list[PackDiagnosis],
    cerbo: CerboReadings,
) -> Optional[CrossCheckResult]:
    """#4: SOC agreement across BMS direct, Cerbo battery service, Cerbo system summary."""
    primary = next((d for d in diagnoses if not d.via_master), diagnoses[0])
    if primary.pack_soc_percent is None:
        return None
    bms_soc = float(primary.pack_soc_percent)
    socs: dict[str, float] = {"BMS direct": bms_soc}
    if cerbo.battery.soc_percent is not None:
        socs["Cerbo battery"] = cerbo.battery.soc_percent
    if cerbo.system.soc_percent is not None:
        socs["Cerbo system"] = cerbo.system.soc_percent
    if len(socs) < 2:
        return None

    values = list(socs.values())
    spread = max(values) - min(values)
    rendered = ", ".join(f"{k} {v:.0f} %" for k, v in socs.items())

    if spread <= 2.0:
        return CrossCheckResult(
            id="soc_parity",
            title="SOC parity",
            severity=SEVERITY_OK,
            detail=f"SOC agrees within {spread:.1f} % across all sources read ({rendered}).",
        )
    return CrossCheckResult(
        id="soc_parity",
        title="SOC mismatch between BMS and Cerbo",
        severity=SEVERITY_WARNING,
        detail=f"SOC differs by {spread:.1f} % between sources: {rendered}. CAN comms may be stale or briefly interrupted.",
        suggestion="Re-seat the CAN cable between the battery and the Cerbo; restart the Cerbo if the gap persists more than a few minutes.",
    )


def _check_imbalance_attribution(
    diagnoses: list[PackDiagnosis],
    cerbo: CerboReadings,
) -> list[CrossCheckResult]:
    """#7: For every DEGRADING/FAILED pack, attribute cause to either real
    imbalance or DVCC-cap restriction. This is the headline cross-check —
    it tells you whether to RMA the pack or change the install config.
    """
    out: list[CrossCheckResult] = []
    cap = cerbo.dvcc.max_charge_voltage_v
    bms_req = cerbo.battery.requested_cvl_v

    for d in diagnoses:
        if d.verdict not in ("DEGRADING", "FAILED"):
            continue
        # Was the verdict triggered by cell-voltage spread?
        spread_caused = any(
            "spread" in r.lower() and "exceeds" in r.lower()
            for r in (d.verdict_reasons or [])
        )
        if not spread_caused:
            continue

        addr = d.address
        if cap is None:
            out.append(CrossCheckResult(
                id=f"imbalance_attribution_pack{addr}",
                title=f"Pack {addr}: imbalance is real",
                severity=SEVERITY_INFO,
                detail=(
                    f"Pack {addr} verdict is {d.verdict} on cell-voltage spread, and DVCC "
                    f"has no charge-voltage cap configured — the BMS is allowed to balance "
                    f"fully. The imbalance reflects genuine pack-side cell behaviour, not a "
                    f"config restriction."
                ),
            ))
            continue

        # Cap is set.
        if bms_req is not None and cap < bms_req - 0.05:
            gap = bms_req - cap
            out.append(CrossCheckResult(
                id=f"imbalance_attribution_pack{addr}",
                title=f"Pack {addr}: imbalance may be config-induced",
                severity=SEVERITY_WARNING,
                detail=(
                    f"Pack {addr} verdict is {d.verdict} on cell-voltage spread, but the "
                    f"DVCC cap ({cap:.1f} V) is {gap:.2f} V below the BMS-requested balance "
                    f"voltage ({bms_req:.1f} V). Cells may be unable to fully balance "
                    f"because the system isn't allowed to reach the BMS-designed top of charge."
                ),
                suggestion=(
                    f"Raise the DVCC cap to at least {bms_req:.1f} V (or remove it) and "
                    f"re-test in 24-48 h. If the verdict persists with the cap lifted, the "
                    f"imbalance is genuine and the pack warrants closer investigation."
                ),
            ))
        else:
            out.append(CrossCheckResult(
                id=f"imbalance_attribution_pack{addr}",
                title=f"Pack {addr}: imbalance is real",
                severity=SEVERITY_INFO,
                detail=(
                    f"Pack {addr} verdict is {d.verdict} on cell-voltage spread. The DVCC "
                    f"cap ({cap:.1f} V) is not below the BMS request — the cap isn't "
                    f"causing the imbalance. The verdict reflects genuine pack-side behaviour."
                ),
            ))
    return out
