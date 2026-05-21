"""Victron Cerbo GX (Venus OS) client — read-only Modbus TCP.

Reads DVCC settings, BMS-relayed values and battery alarms from a Cerbo
GX (or any Venus OS device — Venus GX, Color Control, Octo GX, Ekrano GX)
on the local network. The data is consumed by the cross-check engine to
flag mismatches between what the battery's BMS says directly (via the
existing serial console client) and what the Cerbo has propagated through
DVCC to the rest of the system.

Design choices:
  - Read-only. No write methods are exposed. The tool never mutates a
    Cerbo's configuration; it only inspects it.
  - Discovery via mDNS (`venus.local`) with manual-IP fallback for direct-
    cable or Tailscale/VPN scenarios.
  - The read set is deliberately narrow: only what the cross-check engine
    needs. Full Cerbo register space is wide; we don't try to mirror it.

The register addresses, unit IDs and value scales used below come from
Victron Energy's CCGX-Modbus-TCP-register-list (published spreadsheet).
"""

import logging
import socket
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# Default Modbus TCP port on a Venus OS GX device.
DEFAULT_MODBUS_PORT = 502

# Unit IDs for the dbus services we read from. Venus OS routes Modbus unit
# IDs to internal dbus services:
#   100 — com.victronenergy.system   (DVCC, system-level values)
#   225 — com.victronenergy.battery  (battery service; may vary per install)
#   246 — com.victronenergy.vebus    (Multiplus / Quattro)
# The battery unit ID in particular can shift between installs, so the
# real read path probes it; these are starting defaults.
UNIT_ID_SYSTEM = 100
UNIT_ID_BATTERY = 225
UNIT_ID_VEBUS = 246


# ---------------------------------------------------------------------------
# Data classes — what we extract from a Cerbo, in normalised form.
# ---------------------------------------------------------------------------

@dataclass
class CerboInfo:
    """Identity of the Cerbo we're talking to."""
    host: str
    port: int = DEFAULT_MODBUS_PORT
    product_name: str = ""
    firmware: str = ""
    serial: str = ""


@dataclass
class DvccConfig:
    """DVCC (Distributed Voltage and Current Control) settings.

    These are the install-side policy: what the user/installer has
    configured the Cerbo to do regardless of what the BMS asks.
    """
    enabled: Optional[bool] = None
    controller: Optional[str] = None              # "BMS" / "Battery monitor" / "No external controller" / etc.
    max_charge_voltage_v: Optional[float] = None  # "Limit managed battery charge voltage" cap (None = off)
    max_charge_current_a: Optional[float] = None
    max_discharge_current_a: Optional[float] = None
    svs_enabled: Optional[bool] = None            # Shared Voltage Sense
    shared_current_sense: Optional[bool] = None
    shared_temperature_sense: Optional[bool] = None


@dataclass
class BatteryRelay:
    """Battery state as the Cerbo currently sees it via CAN / DVCC.

    Distinguish from BMS-direct readings (which come from the serial
    console client). Cross-checks compare the two sources.
    """
    product_id: Optional[int] = None
    product_name: str = ""
    is_pylontech: bool = False
    soc_percent: Optional[float] = None
    voltage_v: Optional[float] = None
    current_a: Optional[float] = None
    temperature_c: Optional[float] = None
    requested_cvl_v: Optional[float] = None       # BMS-requested Charge Voltage Limit
    requested_ccl_a: Optional[float] = None       # BMS-requested Charge Current Limit
    requested_dcl_a: Optional[float] = None       # BMS-requested Discharge Current Limit
    alarms_active: list[str] = field(default_factory=list)


@dataclass
class CerboReadings:
    """Complete read snapshot from a Cerbo at one moment in time."""
    info: CerboInfo
    dvcc: DvccConfig = field(default_factory=DvccConfig)
    battery: BatteryRelay = field(default_factory=BatteryRelay)
    read_at: str = ""   # ISO timestamp


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class CerboClient:
    """Modbus TCP client for one Cerbo GX. Read-only by design.

    Lifecycle:
        client = CerboClient()
        ok = client.connect("192.168.1.50")        # or via discover_cerbos()
        if ok:
            readings = client.read_all()
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

        Returns True if the socket opens and a basic identifying read
        succeeds. Sets is_connected accordingly. Live implementation lands
        in the next commit (chunk 2 of the build).
        """
        raise NotImplementedError("Cerbo Modbus client — implementation in chunk 2")

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

    def read_all(self) -> CerboReadings:
        """Read the DVCC config + battery-relay snapshot in one batch.

        Live implementation lands in chunk 2.
        """
        raise NotImplementedError("Cerbo Modbus reads — implementation in chunk 2")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_cerbos(timeout: float = 2.0) -> list[str]:
    """Find Cerbo / Venus GX devices on the local network.

    Returns a list of IPv4 addresses. Empty list if none found within the
    timeout, or if mDNS is blocked by the network (in which case the user
    falls back to manual IP entry in the UI).

    This v1 attempts only the simple `venus.local` socket-level lookup,
    which catches the common case where the Cerbo's mDNS responder has
    advertised the default Venus OS hostname on the LAN. The full
    `zeroconf` service-browse path lands in chunk 2.
    """
    found: list[str] = []
    try:
        host = socket.gethostbyname("venus.local")
        if host:
            found.append(host)
            logger.info(f"Discovered Cerbo via venus.local -> {host}")
    except socket.gaierror:
        logger.debug("venus.local did not resolve via mDNS")
    return found
