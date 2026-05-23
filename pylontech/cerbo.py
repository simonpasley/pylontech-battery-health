"""Victron Cerbo GX (Venus OS) client — read-only Modbus TCP.

Reads DVCC settings, BMS-relayed values and battery state from a Cerbo GX
(or any Venus OS device — Venus GX, Color Control, Octo GX, Ekrano GX)
on the local network. The data is consumed by the cross-check engine to
flag mismatches between what the battery's BMS says directly (via the
existing serial console client) and what the Cerbo has propagated through
DVCC to the rest of the system.

Design choices:
  - Read-only. No write methods are exposed. The tool never mutates a
    Cerbo's configuration; it only inspects it.
  - Discovery via mDNS (`venus.local` + `_workstation._tcp` service browse)
    with manual-IP fallback for direct-cable or Tailscale/VPN scenarios.
  - The read set is deliberately narrow: only what the cross-check engine
    needs. Full Cerbo register space is wide; we don't try to mirror it.

The register addresses, unit IDs and value scales used below come from
Victron Energy's CCGX-Modbus-TCP-register-list (published spreadsheet),
verified empirically against a real Cerbo GX.
"""

import logging
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# Default Modbus TCP port on a Venus OS GX device.
DEFAULT_MODBUS_PORT = 502

# Unit IDs for the dbus services we read from. Venus OS routes Modbus unit
# IDs to internal dbus services:
#   100 — com.victronenergy.system   (DVCC, system-level values)
#   225 — com.victronenergy.battery  (battery service; default for one BMS)
#   246 — com.victronenergy.vebus    (Multiplus / Quattro)
# The battery unit ID can shift on multi-BMS installs but 225 is the
# Venus OS convention for a single primary battery.
UNIT_ID_SYSTEM = 100
UNIT_ID_BATTERY = 225
UNIT_ID_VEBUS = 246

# Battery-service alarm registers (com.victronenergy.battery on unit 225).
# Each is uint16: 0 = OK, 1 = Warning, 2 = Alarm.
# Addresses verified against Victron CCGX Modbus register list v3.73.
_BATTERY_ALARM_REGISTERS = (
    (268, "Low voltage"),
    (269, "High voltage"),
    (272, "Low SOC"),
    (273, "Low temperature"),
    (274, "High temperature"),
    (320, "High charge current"),
    (321, "High discharge current"),
    (322, "Cell imbalance"),
    (323, "Internal failure"),
)


# ---------------------------------------------------------------------------
# Data classes — what we extract from a Cerbo, in normalised form.
# ---------------------------------------------------------------------------

@dataclass
class CerboInfo:
    """Identity of the Cerbo we're talking to."""
    host: str
    port: int = DEFAULT_MODBUS_PORT
    serial: str = ""           # 12-char hex (MAC-derived) from system service reg 800
    firmware: str = ""         # placeholder; not in v1 reads


@dataclass
class DvccConfig:
    """DVCC (Distributed Voltage and Current Control) settings.

    Install-side policy: what the user/installer has configured the Cerbo
    to do regardless of what the BMS asks. Fields are None when the
    corresponding read failed or returned a "not set" sentinel:

      - reg 2705 "DVCC system max charge current" is int16; value -1
        (0xFFFF as uint16) means "disabled" (no charge-current cap).
      - reg 2710 "Limit managed battery voltage" is uint16 scale 10;
        value 0 means "not set" (no charge-voltage cap).

    Register addresses verified against the official Victron CCGX
    Modbus TCP register list v3.73 (Field list sheet).
    """
    max_charge_voltage_v: Optional[float] = None    # reg 2710; None = no cap configured
    max_charge_current_a: Optional[float] = None    # reg 2705; None = disabled


@dataclass
class BatteryRelay:
    """Battery state as the Cerbo sees it via CAN / DVCC.

    Distinct from BMS-direct readings (which come from the serial console
    client). Cross-checks compare the two sources.

    Register addresses verified against Victron CCGX Modbus register
    list v3.73.
    """
    soc_percent: Optional[float] = None            # reg 266, uint16 /10
    voltage_v: Optional[float] = None              # reg 259, uint16 /100
    current_a: Optional[float] = None              # reg 261, int16  /10  (signed; + = charging, - = discharging)
    temperature_c: Optional[float] = None          # reg 262, int16  /10
    requested_cvl_v: Optional[float] = None        # reg 305, uint16 /10  — BMS-requested CVL (None = no active request)
    requested_ccl_a: Optional[float] = None        # reg 307, uint16 /10  — BMS-requested CCL
    requested_dcl_a: Optional[float] = None        # reg 308, uint16 /10  — BMS-requested DCL
    alarms_active: list[str] = field(default_factory=list)   # human-readable list of any non-zero alarms
    connection_info: str = ""                      # reg 1328 (/ConnectionInformation, string[8])


@dataclass
class SystemSummary:
    """System-service (unit 100) summary of the battery as the Cerbo aggregates it.

    Useful for cross-checking against the battery-service (unit 225) values:
    if these diverge meaningfully, that's a CAN-comms gap signal.
    """
    voltage_v: Optional[float] = None
    current_a: Optional[float] = None
    power_w: Optional[float] = None
    soc_percent: Optional[float] = None


@dataclass
class CerboReadings:
    """Complete read snapshot from a Cerbo at one moment in time."""
    info: CerboInfo
    dvcc: DvccConfig = field(default_factory=DvccConfig)
    battery: BatteryRelay = field(default_factory=BatteryRelay)
    system: SystemSummary = field(default_factory=SystemSummary)
    read_at: str = ""   # ISO timestamp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _s16(value: int) -> int:
    """Re-interpret a 16-bit unsigned register as signed."""
    return value - 65536 if value > 32767 else value


def _none_if_sentinel(value: int, sentinel: int = 0xFFFF) -> Optional[int]:
    """Translate Venus OS 'not set' sentinel into Python None."""
    return None if value == sentinel else value


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class CerboClient:
    """Modbus TCP client for one Cerbo GX. Read-only by design.

    Lifecycle:
        client = CerboClient()
        ok = client.connect("192.168.1.50")
        if ok:
            readings = client.read_all()
            print(readings.battery.soc_percent)
            client.disconnect()

    No write methods are exposed; this client cannot mutate Cerbo state.
    """

    def __init__(self):
        self._client = None
        self._host: str = ""
        self._port: int = DEFAULT_MODBUS_PORT
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def connect(self, host: str, port: int = DEFAULT_MODBUS_PORT,
                timeout: float = 3.0) -> bool:
        """Open a Modbus TCP connection to the Cerbo.

        Returns True if the socket opens and a sanity-check read succeeds.
        Sets is_connected accordingly.
        """
        # Lazy import so the rest of the module is importable even if
        # pymodbus isn't installed yet (matters for early dev / scaffold).
        from pymodbus.client import ModbusTcpClient

        self.disconnect()
        self._host = host
        self._port = port
        self._client = ModbusTcpClient(host, port=port, timeout=timeout)
        try:
            ok = self._client.connect()
        except Exception as e:
            logger.error(f"Cerbo TCP connect to {host}:{port} failed: {e}")
            self._client = None
            return False
        if not ok:
            logger.error(f"Cerbo TCP connect to {host}:{port} returned False")
            self._client = None
            return False

        # Sanity-check read: system serial (reg 800, unit 100). Confirms
        # Modbus is enabled and the unit responds.
        try:
            r = self._client.read_holding_registers(address=800, count=6,
                                                    device_id=UNIT_ID_SYSTEM)
            if r.isError():
                logger.error(f"Cerbo identity read failed: {r}")
                self._client.close()
                self._client = None
                return False
        except Exception as e:
            logger.error(f"Cerbo identity read raised: {e}")
            self._client.close()
            self._client = None
            return False

        self._connected = True
        logger.info(f"Connected to Cerbo at {host}:{port}")
        return True

    def disconnect(self) -> None:
        """Close the Modbus connection. Safe to call when not connected."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception as e:
                logger.warning(f"Error closing Cerbo Modbus client: {e}")
        self._client = None
        self._connected = False
        self._host = ""

    # --- low-level helpers ------------------------------------------------

    def _read(self, addr: int, count: int, unit: int) -> Optional[list[int]]:
        if not self._connected or self._client is None:
            return None
        try:
            r = self._client.read_holding_registers(address=addr, count=count,
                                                    device_id=unit)
            if r.isError():
                logger.debug(f"Read addr={addr} count={count} unit={unit} -> {r}")
                return None
            return list(r.registers)
        except Exception as e:
            logger.debug(f"Read addr={addr} unit={unit} raised: {e}")
            return None

    def _reg(self, addr: int, unit: int) -> Optional[int]:
        """Read one register, return value or None on error."""
        regs = self._read(addr, 1, unit)
        return regs[0] if regs else None

    # --- public read ------------------------------------------------------

    def read_all(self) -> CerboReadings:
        """Read DVCC config + battery-relay + system summary in one batch.

        Returns a CerboReadings dataclass with any unreadable fields left
        as None / empty. The cross-check engine handles partial reads
        gracefully.
        """
        info = CerboInfo(host=self._host, port=self._port)

        # System serial (12 hex chars from 6 registers, big-endian ASCII pairs).
        serial_regs = self._read(800, 6, UNIT_ID_SYSTEM)
        if serial_regs:
            chars = []
            for r in serial_regs:
                chars.append(chr((r >> 8) & 0xFF))
                chars.append(chr(r & 0xFF))
            info.serial = "".join(c for c in chars if c.isprintable()).strip()

        # Battery service (unit 225) — register addresses verified against
        # Victron CCGX Modbus register list v3.73.
        battery = BatteryRelay()
        v = self._reg(259, UNIT_ID_BATTERY)
        if v is not None:
            battery.voltage_v = v / 100.0
        i = self._reg(261, UNIT_ID_BATTERY)
        if i is not None:
            battery.current_a = _s16(i) / 10.0
        t = self._reg(262, UNIT_ID_BATTERY)
        if t is not None:
            battery.temperature_c = _s16(t) / 10.0
        soc = self._reg(266, UNIT_ID_BATTERY)
        if soc is not None:
            battery.soc_percent = soc / 10.0
        # CVL is reg 305 (/Info/MaxChargeVoltage). 0 = BMS not currently
        # requesting a specific charge voltage (typical when discharging).
        cvl = self._reg(305, UNIT_ID_BATTERY)
        if cvl is not None and cvl != 0:
            battery.requested_cvl_v = cvl / 10.0
        ccl = self._reg(307, UNIT_ID_BATTERY)
        if ccl is not None:
            battery.requested_ccl_a = ccl / 10.0
        dcl = self._reg(308, UNIT_ID_BATTERY)
        if dcl is not None:
            battery.requested_dcl_a = dcl / 10.0

        # Battery alarm registers (unit 225). Each is uint16 where 0 = OK,
        # 1 = Warning, 2 = Alarm. We record any non-zero alarm by its
        # human-readable name.
        for reg, name in _BATTERY_ALARM_REGISTERS:
            value = self._reg(reg, UNIT_ID_BATTERY)
            if value is not None and value > 0:
                battery.alarms_active.append(
                    name + (" (warning)" if value == 1 else "")
                )

        # /ConnectionInformation (reg 1328, string[8]). For Pylontech BMSes
        # this is typically empty; some other vendors populate it. Used as
        # a best-effort brand hint when there's no direct serial connection.
        conn_regs = self._read(1328, 8, UNIT_ID_BATTERY)
        if conn_regs:
            chars = []
            for r in conn_regs:
                chars.append(chr((r >> 8) & 0xFF))
                chars.append(chr(r & 0xFF))
            battery.connection_info = "".join(
                c for c in chars if c.isprintable()
            ).strip()

        # System summary (unit 100).
        system = SystemSummary()
        sv = self._reg(840, UNIT_ID_SYSTEM)
        if sv is not None:
            system.voltage_v = sv / 10.0
        si = self._reg(841, UNIT_ID_SYSTEM)
        if si is not None:
            system.current_a = _s16(si) / 10.0
        sw = self._reg(842, UNIT_ID_SYSTEM)
        if sw is not None:
            system.power_w = float(_s16(sw))
        ssoc = self._reg(843, UNIT_ID_SYSTEM)
        if ssoc is not None:
            system.soc_percent = float(ssoc)

        # DVCC settings (unit 100), addresses verified:
        #   reg 2705 = DVCC system max charge current (int16, A, -1 = disabled)
        #   reg 2710 = Limit managed battery voltage (uint16 /10, V, 0 = not set)
        dvcc = DvccConfig()
        cap_i_raw = self._reg(2705, UNIT_ID_SYSTEM)
        if cap_i_raw is not None:
            signed = _s16(cap_i_raw)
            if signed >= 0:
                dvcc.max_charge_current_a = float(signed)
            # else: -1 means disabled; leave field as None
        cap_v_raw = self._reg(2710, UNIT_ID_SYSTEM)
        if cap_v_raw is not None and cap_v_raw > 0:
            dvcc.max_charge_voltage_v = cap_v_raw / 10.0
        # else: 0 means "not set" / no cap configured; field stays None

        return CerboReadings(
            info=info,
            dvcc=dvcc,
            battery=battery,
            system=system,
            read_at=datetime.now().isoformat(timespec="seconds"),
        )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _venus_local_lookup() -> Optional[str]:
    """Resolve venus.local via the OS resolver (works on most LANs with mDNS)."""
    try:
        host = socket.gethostbyname("venus.local")
        return host or None
    except socket.gaierror:
        return None


def discover_cerbos(timeout: float = 2.0) -> list[str]:
    """Find Cerbo / Venus GX devices on the local network.

    Returns a list of IPv4 addresses. Empty list if none found within the
    timeout, or if mDNS is blocked by the network (UI then falls back to
    manual IP entry).

    Strategy:
      1. Quick venus.local OS-level lookup (catches the common case).
      2. zeroconf service-browse for _workstation._tcp services whose
         instance name contains 'venus' or 'cerbo' (catches renamed Cerbos
         and multi-Cerbo installs).
    """
    found: list[str] = []
    seen: set[str] = set()

    ip = _venus_local_lookup()
    if ip:
        found.append(ip)
        seen.add(ip)

    # Lazy import — zeroconf is heavy. Don't load it if we got an answer
    # already and the user only wanted the quick path.
    try:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
    except ImportError:
        logger.debug("zeroconf not installed; mDNS browse skipped")
        return found

    class _Listener(ServiceListener):
        def __init__(self):
            self.hits: list[str] = []

        def add_service(self, zc, type_, name):
            try:
                info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
                if info is None:
                    return
                lname = name.lower()
                if "venus" in lname or "cerbo" in lname:
                    for addr in info.parsed_scoped_addresses() or info.parsed_addresses():
                        if "." in addr:  # IPv4
                            self.hits.append(addr)
            except Exception as e:
                logger.debug(f"zeroconf get_service_info error: {e}")

        def remove_service(self, zc, type_, name):
            pass

        def update_service(self, zc, type_, name):
            pass

    try:
        zc = Zeroconf()
        listener = _Listener()
        ServiceBrowser(zc, "_workstation._tcp.local.", listener)
        # Brief wait — zeroconf is async by nature.
        import time as _time
        _time.sleep(max(0.5, min(timeout, 3.0)))
        for addr in listener.hits:
            if addr not in seen:
                found.append(addr)
                seen.add(addr)
        zc.close()
    except Exception as e:
        logger.debug(f"zeroconf browse failed: {e}")

    return found
