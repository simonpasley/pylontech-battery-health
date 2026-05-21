"""Battery-health diagnostic for Pylontech battery packs.

Runs the full diagnostic command set against a pack and returns structured
data with a built-in health verdict. The interpretation is encoded in code
so the tool works without prior expert knowledge — installers do not need
to know which commands matter or what the numbers mean.

Tested against US3000C and US5000 modules (firmware B69.x).
"""

import re
from dataclasses import dataclass, field
from typing import Optional


# Cell-voltage spread thresholds (mV) under load. Healthy LFP packs at
# moderate current sit well under 30 mV; 30–50 mV indicates emerging
# imbalance / weakening cell; > 50 mV is a clear failure signature.
SPREAD_HEALTHY_MAX = 30
SPREAD_DEGRADING_MAX = 50

# Idle-plateau cell-voltage spread thresholds (mV). At rest in the flat
# part of the LFP discharge curve, cells should sit *tighter* than under
# load because there is no load-induced IR scatter. So the idle bands are
# proportionately smaller. These let the tool deliver a verdict on a pack
# that's quietly sitting in the inverter loop rather than always punting
# to UNKNOWN.
IDLE_SPREAD_HEALTHY_MAX = 20
IDLE_SPREAD_DEGRADING_MAX = 40

# Measurement-validity envelope. Cell-voltage spread is only meaningful
# inside one of two windows:
#   (a) Under load: |I| >= 200 mA, SOC 15–92 %, temp > 5 °C.
#       Uses SPREAD_HEALTHY_MAX / SPREAD_DEGRADING_MAX.
#   (b) Idle on the LFP plateau: |I| < 200 mA, SOC 10–85 %, temp > 5 °C.
#       Uses IDLE_SPREAD_HEALTHY_MAX / IDLE_SPREAD_DEGRADING_MAX.
# Outside both, the verdict goes to UNKNOWN with a hint of what to re-test:
# CV-tail (top), near-empty (bottom), or cold pack distort cell voltages
# mechanically rather than indicating real imbalance.
VALID_MIN_CURRENT_MA = 200       # |I| >= 0.2 A separates "loaded" from "idle"
VALID_SOC_MIN = 15               # %  — loaded-mode window lower bound
VALID_SOC_MAX = 92               # %  — loaded-mode window upper bound
IDLE_SOC_MIN = 10                # %  — idle-mode window lower bound
IDLE_SOC_MAX = 85                # %  — idle-mode window upper bound (avoid CV tail)
VALID_TEMP_MIN_MC = 5000         # 5 °C in milli-celsius — applies to both modes


@dataclass
class CellReading:
    cell_number: int          # 0-indexed as Pylontech reports
    voltage_mv: int = 0
    current_ma: int = 0
    temp_mc: int = 0          # milli-celsius
    soc_percent: int = 0
    coulomb_mah: int = 0
    soh_count: int = 0        # SOHCount column from `soh N`
    soh_status: str = "Normal"


@dataclass
class PackStats:
    """Lifetime counters from `stat`. `soh_percent=None` means not captured
    (e.g. queried via master comm bus where `stat` is pack-local)."""
    soh_percent: Optional[int] = None  # BMS-reported SOH (0 = end-of-life)
    soh_abnormal_events: int = 0  # `SOH Times`
    real_cycles: int = 0          # `CYCLE Times` — the meaningful real-cycle count
    charge_transitions: int = 0   # `Charge Times` — misleading partial-event counter
    discharge_count: int = 0
    bat_ov_count: int = 0         # cell over-voltage protection trips
    bat_hv_count: int = 0         # cell high-voltage warnings
    bat_lv_count: int = 0         # cell low-voltage warnings
    bat_uv_count: int = 0
    coc_count: int = 0
    doc_count: int = 0
    sc_count: int = 0
    shut_count: int = 0
    reset_count: int = 0


@dataclass
class PackDiagnosis:
    pack_id: int
    address: int
    barcode: str = ""
    model: str = ""
    spec: str = ""
    release_date: str = ""
    main_soft_version: str = ""
    soft_version: str = ""
    cell_count: int = 15

    pack_voltage_mv: int = 0
    pack_current_ma: int = 0
    pack_soc_percent: int = 0
    pack_temp_mc: int = 0
    runtime_state: str = "Unknown"
    runtime_volt_state: str = "Normal"
    runtime_soh_status: str = "Normal"

    cells: list[CellReading] = field(default_factory=list)
    stats: PackStats = field(default_factory=PackStats)

    most_recent_event: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)  # raw command outputs for traceability

    # Capture-context flag — True when this pack was queried via the master's
    # comm bus rather than directly. In that mode `stat` and `data event` are
    # skipped because they would return master-pack data not this pack's.
    via_master: bool = False

    # Computed verdict
    spread_mv: int = 0
    weakest_cell: Optional[int] = None
    abnormal_cells: list[int] = field(default_factory=list)
    verdict: str = "UNKNOWN"             # HEALTHY / DEGRADING / FAILED / UNKNOWN
    verdict_reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsers — one per Pylontech native console command
# ---------------------------------------------------------------------------

def parse_info(text: str) -> dict:
    """Parse `info [N]` output."""
    out = {}
    for line in text.splitlines():
        if ':' not in line:
            continue
        k, _, v = line.partition(':')
        k = k.strip().lower()
        v = v.strip()
        if not v:
            continue
        if k.startswith('manufacturer'):
            out['manufacturer'] = v
        elif k.startswith('device name') or k == 'model':
            out['model'] = v
        elif k.startswith('barcode'):
            out['barcode'] = v
        elif k.startswith('release date'):
            out['release_date'] = v
        elif k.startswith('main soft'):
            out['main_soft_version'] = v
        elif k.startswith('soft  version') or (k.startswith('soft') and 'version' in k and 'main' not in k and 'boot' not in k and 'comm' not in k):
            out['soft_version'] = v
        elif k.startswith('specification'):
            out['spec'] = v
        elif k.startswith('cell number') or k.startswith('cell count'):
            try:
                out['cell_count'] = int(v)
            except ValueError:
                pass
        elif k.startswith('device address'):
            try:
                out['address'] = int(v)
            except ValueError:
                pass
    return out


def parse_pwr_single(text: str) -> dict:
    """Parse single-pack `pwr N` output (key:value rows)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if ':' not in line:
            continue
        k, _, v = line.partition(':')
        k = k.strip()
        v = v.strip()
        m = re.match(r'^(-?\d+)', v)
        num = int(m.group(1)) if m else None
        if k == 'Voltage' and num is not None:
            out['voltage_mv'] = num
        elif k == 'Current' and num is not None:
            out['current_ma'] = num
        elif k == 'Temperature' and num is not None:
            out['temp_mc'] = num
        elif k == 'Coulomb' and num is not None:
            out['soc_percent'] = num
        elif k == 'Total Coulomb' and num is not None:
            out['total_capacity_mah'] = num
        elif k == 'Charge Times' and num is not None:
            out['charge_transitions'] = num
        elif k == 'Basic Status':
            out['state'] = v
        elif k == 'Volt Status':
            out['volt_status'] = v
        elif k == 'Soh. Status':
            out['soh_status'] = v
        elif k == 'Heater Status':
            out['heater'] = v
    return out


def parse_bat(text: str) -> list[CellReading]:
    """Parse `bat N` output — per-cell voltages.

    Defensive against firmware variation: locates columns by header name
    rather than position so a leading balance-flag column or extra prefix
    won't desync the rest. Falls back to positional parsing if the header
    can't be identified.
    """
    cells: list[CellReading] = []
    header_cols: list[str] = []
    in_data = False
    for line in text.splitlines():
        if not in_data and 'Battery' in line and 'Volt' in line and 'Curr' in line:
            header_cols = [h.strip() for h in line.split() if h.strip()]
            in_data = True
            continue
        if not in_data:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        # Skip rows that don't start with a plausible cell index (1..30).
        try:
            cell_num = int(parts[0])
        except ValueError:
            continue
        if not (0 <= cell_num <= 30):
            continue
        # Map columns by header name when we have one, else fall back to
        # the historical positional layout.
        def col(name: str, default=None):
            if not header_cols:
                return default
            for i, h in enumerate(header_cols):
                if h.lower().startswith(name.lower()):
                    if i < len(parts):
                        return parts[i]
            return default
        try:
            volt_mv = int(col('Volt', parts[1]))
            curr_ma = int(col('Curr', parts[2]))
            temp_mc = int(col('Tempr', parts[3]))
        except (ValueError, TypeError):
            continue
        if not (2000 <= volt_mv <= 4500):
            continue
        cell = CellReading(cell_number=cell_num, voltage_mv=volt_mv,
                           current_ma=curr_ma, temp_mc=temp_mc)
        for p in parts[4:]:
            if p.endswith('%'):
                try:
                    cell.soc_percent = int(p[:-1])
                except ValueError:
                    pass
            elif p.isdigit():
                try:
                    val = int(p)
                    if 1000 <= val <= 200000:
                        cell.coulomb_mah = val
                except ValueError:
                    pass
        cells.append(cell)
    return cells


def parse_soh(text: str) -> dict[int, dict]:
    """Parse `soh N` output — per-cell SOHCount and SOHStatus."""
    out: dict[int, dict] = {}
    in_data = False
    for line in text.splitlines():
        if 'Battery' in line and 'SOHCount' in line:
            in_data = True
            continue
        if not in_data:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            cell_num = int(parts[0])
            voltage_mv = int(parts[1])
            count = int(parts[2])
        except ValueError:
            continue
        status = parts[3]
        out[cell_num] = {
            'voltage_mv': voltage_mv,
            'soh_count': count,
            'soh_status': status,
        }
    return out


def parse_stat(text: str) -> dict:
    """Parse `stat` (or `stat N`) output — lifetime counters."""
    out: dict = {}
    key_map = {
        'SOH': 'soh_percent',
        'SOH Times': 'soh_abnormal_events',
        'CYCLE Times': 'real_cycles',
        'Charge Times': 'charge_transitions',
        'Discharge Cnt.': 'discharge_count',
        'Bat OV Times': 'bat_ov_count',
        'Bat HV Times': 'bat_hv_count',
        'Bat LV Times': 'bat_lv_count',
        'Bat UV Times': 'bat_uv_count',
        'COC Times': 'coc_count',
        'DOC Times': 'doc_count',
        'SC Times': 'sc_count',
        'Shut Times': 'shut_count',
        'Reset Times': 'reset_count',
    }
    for line in text.splitlines():
        if ':' not in line:
            continue
        k, _, v = line.partition(':')
        k = k.strip()
        v = v.strip()
        try:
            num = int(v)
        except ValueError:
            continue
        if k in key_map:
            out[key_map[k]] = num
    return out


def parse_data_event(text: str) -> dict:
    """Parse `data event` output — most recent stored event."""
    out = {'header': {}, 'cells': []}
    in_cell_table = False
    for line in text.splitlines():
        line = line.strip()
        if 'Battery' in line and 'Volt' in line and 'Coulomb' in line:
            in_cell_table = True
            continue
        if not in_cell_table:
            if ':' in line and not line.startswith('-'):
                k, _, v = line.partition(':')
                out['header'][k.strip()] = v.strip()
        else:
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                cell_num = int(parts[0])
                volt_mv = int(parts[1])
            except ValueError:
                continue
            if not (2000 <= volt_mv <= 4500):
                continue
            cell = {'cell': cell_num, 'voltage_mv': volt_mv}
            for p in parts:
                if p.endswith('%'):
                    try:
                        cell['coulomb_percent'] = int(p[:-1])
                    except ValueError:
                        pass
            out['cells'].append(cell)
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def parse_rack_pwr(text: str) -> list[int]:
    """Return list of pack addresses that appear online in a multi-pack
    `pwr` table (when run from the master)."""
    addresses = []
    in_data = False
    for line in text.splitlines():
        if not in_data and 'Power' in line and 'Volt' in line and 'Curr' in line:
            in_data = True
            continue
        if not in_data:
            continue
        line_strip = line.strip()
        if not line_strip or line_strip.startswith(('Command', '$')):
            continue
        if 'Absent' in line:
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            addr = int(parts[0])
            # Sanity: pack address should be in 1..16
            if 1 <= addr <= 16 and len(parts) >= 4:
                addresses.append(addr)
        except ValueError:
            continue
    return addresses


def diagnose_pack(console, address: int, via_master: bool = False) -> PackDiagnosis:
    """Run the full diagnostic command set on a pack and return a verdict.

    When via_master=True (i.e. the cable is on a different pack than this
    one and we're querying via the comm bus), the pack-local commands
    `stat` and `data event` are skipped because they would return the
    master's data rather than this slave's data.
    """
    diag = PackDiagnosis(pack_id=address, address=address, via_master=via_master)

    cmds = {
        'info': f'info {address}',
        'pwr': f'pwr {address}',
        'bat': f'bat {address}',
    }
    if not via_master:
        # `soh N` is pack-local on this firmware — it returns "Unknown
        # command" when issued for a slave via the master's comm bus.
        cmds['soh'] = f'soh {address}'
        cmds['stat'] = 'stat' if address == 1 else f'stat {address}'
        cmds['data_event'] = 'data event'

    for label, cmd in cmds.items():
        diag.raw[label] = console._send(cmd, timeout=15)
    if via_master:
        skipped = ('# Skipped — pack-local command, only returns data for the '
                   'pack the cable is plugged into.\n# Connect cable directly '
                   'to this pack to capture per-pack lifetime data.')
        diag.raw['soh'] = skipped
        diag.raw['stat'] = skipped
        diag.raw['data_event'] = skipped

    info = parse_info(diag.raw['info'])
    diag.barcode = info.get('barcode', '')
    diag.model = info.get('model', '')
    diag.spec = info.get('spec', '')
    diag.release_date = info.get('release_date', '')
    diag.main_soft_version = info.get('main_soft_version', '')
    diag.soft_version = info.get('soft_version', '')
    diag.cell_count = info.get('cell_count', 15)

    pwr = parse_pwr_single(diag.raw['pwr'])
    diag.pack_voltage_mv = pwr.get('voltage_mv', 0)
    diag.pack_current_ma = pwr.get('current_ma', 0)
    diag.pack_temp_mc = pwr.get('temp_mc', 0)
    diag.pack_soc_percent = pwr.get('soc_percent', 0)
    diag.runtime_state = pwr.get('state', 'Unknown')
    diag.runtime_volt_state = pwr.get('volt_status', 'Normal')
    diag.runtime_soh_status = pwr.get('soh_status', 'Normal')

    diag.cells = parse_bat(diag.raw['bat'])
    soh_data = parse_soh(diag.raw['soh'])
    for cell in diag.cells:
        if cell.cell_number in soh_data:
            cell.soh_count = soh_data[cell.cell_number]['soh_count']
            cell.soh_status = soh_data[cell.cell_number]['soh_status']

    stat = parse_stat(diag.raw['stat']) if not via_master else {}
    diag.stats = PackStats(
        soh_percent=stat.get('soh_percent'),
        soh_abnormal_events=stat.get('soh_abnormal_events', 0),
        real_cycles=stat.get('real_cycles', 0),
        charge_transitions=stat.get('charge_transitions', 0),
        discharge_count=stat.get('discharge_count', 0),
        bat_ov_count=stat.get('bat_ov_count', 0),
        bat_hv_count=stat.get('bat_hv_count', 0),
        bat_lv_count=stat.get('bat_lv_count', 0),
        bat_uv_count=stat.get('bat_uv_count', 0),
        coc_count=stat.get('coc_count', 0),
        doc_count=stat.get('doc_count', 0),
        sc_count=stat.get('sc_count', 0),
        shut_count=stat.get('shut_count', 0),
        reset_count=stat.get('reset_count', 0),
    )

    diag.most_recent_event = parse_data_event(diag.raw['data_event'])

    if diag.cells:
        voltages = [c.voltage_mv for c in diag.cells]
        diag.spread_mv = max(voltages) - min(voltages)
        # Only flag a weakest cell once the spread is meaningful — otherwise
        # the lowest cell in a healthy pack is just normal cell-to-cell scatter.
        if diag.spread_mv > SPREAD_HEALTHY_MAX:
            weakest = min(diag.cells, key=lambda c: c.voltage_mv)
            diag.weakest_cell = weakest.cell_number

    diag.abnormal_cells = sorted(
        c.cell_number for c in diag.cells
        if c.soh_count > 0 or c.soh_status.strip().lower() == 'abnormal'
    )

    verdict = "HEALTHY"
    reasons: list[str] = []

    # ----- Sanity gate 1: did we get any data at all? --------------------
    # A cable-pull, a wrong-port connection, or a comms timeout produces
    # an empty or partial response. If no `info` and/or no cells came back
    # we cannot judge the pack — UNKNOWN, not a false HEALTHY.
    info_missing = not (diag.raw.get('info') or '').strip()
    no_cells = not diag.cells
    expected = diag.cell_count if diag.cell_count else 15
    cell_short = bool(diag.cells) and expected and len(diag.cells) < expected

    if info_missing or no_cells or cell_short:
        verdict = "UNKNOWN"
        if info_missing:
            reasons.append("No `info` response captured — likely comms failure or cable disconnected")
        if no_cells:
            reasons.append("No per-cell voltages parsed from `bat` — cannot assess pack health")
        elif cell_short:
            reasons.append(
                f"Only {len(diag.cells)} of {expected} expected cells parsed — comms loss or firmware mismatch; "
                "verdict suppressed"
            )
        diag.verdict = verdict
        diag.verdict_reasons = reasons
        return diag

    # ----- Hard failure flags from the BMS itself ------------------------
    if diag.runtime_soh_status.strip().lower() == 'abnormal':
        verdict = "FAILED"
        reasons.append("BMS reports `Soh. Status: Abnormal` in real-time `pwr` status")

    if diag.stats.soh_percent is not None and diag.stats.soh_percent <= 0:
        verdict = "FAILED"
        reasons.append(
            f"BMS-reported SOH = {diag.stats.soh_percent} % — pack declared end-of-life by its own BMS"
        )

    # ----- Sanity gate 2: are measurement conditions valid? --------------
    # Cell-voltage spread is diagnostic in two windows: under load at
    # moderate SOC, OR idle on the flat LFP plateau. Outside both, the
    # spread isn't physically meaningful (CV-tail, near-empty, cold) so
    # we don't apply the spread thresholds — but we DO still apply
    # BMS-reported flags above.
    abs_current = abs(diag.pack_current_ma)
    soc = diag.pack_soc_percent
    temp_mc = diag.pack_temp_mc
    under_load = abs_current >= VALID_MIN_CURRENT_MA

    # Pick the right diagnostic window and spread thresholds for the mode.
    if under_load:
        mode_desc = "under load"
        soc_lo, soc_hi = VALID_SOC_MIN, VALID_SOC_MAX
        healthy_max, degrading_max = SPREAD_HEALTHY_MAX, SPREAD_DEGRADING_MAX
    else:
        mode_desc = "idle on LFP plateau"
        soc_lo, soc_hi = IDLE_SOC_MIN, IDLE_SOC_MAX
        healthy_max, degrading_max = IDLE_SPREAD_HEALTHY_MAX, IDLE_SPREAD_DEGRADING_MAX

    invalid_reasons = []
    if not (soc_lo <= soc <= soc_hi):
        if under_load:
            invalid_reasons.append(
                f"SOC {soc} % is outside the {soc_lo}–{soc_hi} % validity window "
                f"({mode_desc} — CV-tail or near-empty distort spread)"
            )
        else:
            invalid_reasons.append(
                f"Pack is idle and SOC {soc} % is outside the idle-plateau diagnostic "
                f"window {soc_lo}–{soc_hi} %. Re-test under load (|I| ≥ "
                f"{VALID_MIN_CURRENT_MA/1000:.1f} A) or wait until SOC settles in the plateau."
            )
    if temp_mc and temp_mc < VALID_TEMP_MIN_MC:
        invalid_reasons.append(
            f"Pack temperature {temp_mc/1000:.1f} °C is below {VALID_TEMP_MIN_MC/1000:.0f} °C "
            f"(cold cells diverge mechanically — not a real imbalance)"
        )
    conditions_valid = not invalid_reasons

    if not conditions_valid:
        # Don't apply spread-based verdicts. If nothing else flagged the
        # pack, downgrade to UNKNOWN with an explanation so the user re-tests.
        if verdict == "HEALTHY":
            verdict = "UNKNOWN"
            reasons.append(
                f"Cell-voltage spread = {diag.spread_mv} mV, but conditions are not "
                f"valid for a confident verdict: " + "; ".join(invalid_reasons)
            )
    else:
        if diag.spread_mv > degrading_max:
            verdict = "FAILED"
            reasons.append(
                f"Cell-voltage spread {diag.spread_mv} mV exceeds {degrading_max} mV "
                f"failure threshold ({mode_desc})"
            )
        elif diag.spread_mv > healthy_max:
            if verdict == "HEALTHY":
                verdict = "DEGRADING"
            reasons.append(
                f"Cell-voltage spread {diag.spread_mv} mV exceeds {healthy_max} mV "
                f"healthy threshold ({mode_desc})"
            )

    if diag.abnormal_cells:
        if verdict in ("HEALTHY", "UNKNOWN"):
            verdict = "DEGRADING"
        cells_str = ", ".join(f"cell {c}" for c in diag.abnormal_cells)
        reasons.append(f"{len(diag.abnormal_cells)} cell(s) flagged with SOH-abnormal events: {cells_str}")

    if diag.stats.soh_abnormal_events >= 100:
        if verdict in ("HEALTHY", "UNKNOWN"):
            verdict = "DEGRADING"
        reasons.append(
            f"{diag.stats.soh_abnormal_events} cumulative SOH-abnormal events recorded — sustained degradation"
        )

    diag.verdict = verdict
    diag.verdict_reasons = reasons
    return diag


def scan_rack(console, progress_cb=None) -> tuple[list[PackDiagnosis], str]:
    """Scan every online pack in the rack from the master.

    1. Reads the multi-pack `pwr` table from the master to discover online packs
    2. For each pack, runs the health-relevant command set that propagates
       across the comm bus (info, pwr, bat, soh)
    3. The pack we are physically connected to (typically the master, address 1)
       gets the FULL diagnostic including stat and data event
    4. Slaves are flagged via_master=True so the report shows lifetime stats
       and event history as "not captured — direct connection required"

    Returns (list of PackDiagnosis, raw rack pwr text). progress_cb(current,
    total) is called as each pack completes.
    """
    rack_raw = console._send('pwr', timeout=20)
    addresses = parse_rack_pwr(rack_raw)

    if progress_cb:
        progress_cb(0, len(addresses))

    # Determine which address the cable is physically connected to
    info_raw = console._send('info', timeout=10)
    local_info = parse_info(info_raw)
    local_address = local_info.get('address', 1)

    # Defensive: if the rack `pwr` parse missed the local pack (e.g. cable
    # is on a slave with the master offline), include it explicitly so we
    # don't silently treat every pack as via_master and miss its lifetime
    # statistics.
    if local_address and local_address not in addresses:
        addresses.append(local_address)

    diagnoses: list[PackDiagnosis] = []
    for i, addr in enumerate(addresses, start=1):
        is_local = (addr == local_address)
        diag = diagnose_pack(console, addr, via_master=not is_local)
        diagnoses.append(diag)
        if progress_cb:
            progress_cb(i, len(addresses))

    return diagnoses, rack_raw
