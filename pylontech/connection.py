"""Serial port management for Pylontech batteries via USB-to-RS232 adapter."""

import glob
import logging
import re
import threading
import time

import serial

logger = logging.getLogger(__name__)

# Prefer /dev/cu.* ports on macOS (/dev/tty.* waits for DCD signal which
# Pylontech batteries don't provide, causing read hangs).
# List cu.* patterns first so they appear at the top of the port selector.
SERIAL_PORT_PATTERNS = [
    # macOS
    '/dev/cu.wchusbserial*',
    '/dev/cu.usbserial*',
    '/dev/cu.SLAB_USBtoUART*',
    '/dev/cu.usbmodem*',
    '/dev/tty.wchusbserial*',
    '/dev/tty.usbserial*',
    '/dev/tty.SLAB_USBtoUART*',
    '/dev/tty.usbmodem*',
    # Linux (Raspberry Pi etc.)
    '/dev/ttyUSB*',
    '/dev/ttyACM*',
]
# Backwards-compat alias
MACOS_PORT_PATTERNS = SERIAL_PORT_PATTERNS

# Pylontech wakeup frame - sent at 1200 baud to wake battery from sleep.
# This is an RS-485 protocol frame: ~20014682C0048520FCC3\r
WAKEUP_FRAME = bytes([
    0x7E,  # ~ SOI
    0x32, 0x30,  # VER: 20
    0x30, 0x31,  # ADR: 01
    0x34, 0x36,  # CID1: 46
    0x38, 0x32,  # CID2: 82
    0x43, 0x30, 0x30, 0x34,  # LENGTH: C004
    0x38, 0x35, 0x32, 0x30,  # INFO: 8520
    0x46, 0x43, 0x43, 0x33,  # CHKSUM: FCC3
    0x0D,  # CR EOI
])

# Default serial settings for Pylontech console
DEFAULT_BAUD = 115200
DEFAULT_BYTESIZE = serial.EIGHTBITS
DEFAULT_PARITY = serial.PARITY_NONE
DEFAULT_STOPBITS = serial.STOPBITS_ONE
DEFAULT_TIMEOUT = 2.0


class ConnectionManager:
    """Thread-safe serial connection manager for Pylontech batteries."""

    def __init__(self):
        self._serial: serial.Serial | None = None
        self._lock = threading.Lock()
        self._port: str = ''
        self._baud: int = DEFAULT_BAUD
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._serial is not None and self._serial.is_open

    @property
    def port(self) -> str:
        return self._port

    @property
    def baud(self) -> int:
        return self._baud

    @staticmethod
    def detect_ports() -> list[dict]:
        """Auto-detect USB-RS232/RS485 serial ports on macOS and Linux.

        Returns list of dicts with 'port' and 'description' keys.
        """
        found = []
        seen = set()
        for pattern in SERIAL_PORT_PATTERNS:
            for port_path in sorted(glob.glob(pattern)):
                if port_path not in seen:
                    seen.add(port_path)
                    lower = port_path.lower()
                    if 'wch' in lower:
                        desc = 'CH341 USB-RS485'
                    elif 'ttyusb' in lower or 'ttyacm' in lower:
                        desc = 'USB Serial (Linux)'
                    else:
                        desc = 'USB Serial'
                    found.append({'port': port_path, 'description': desc})

        # Also try pyserial's port listing as fallback. On Windows the glob
        # patterns above never match (COM ports aren't filesystem paths), so
        # this fallback is the only port-discovery path for Windows users.
        if not found:
            try:
                from serial.tools import list_ports
                accepted_prefixes = (
                    '/dev/tty.', '/dev/cu.',           # macOS
                    '/dev/ttyUSB', '/dev/ttyACM',      # Linux
                    '/dev/serial/',                    # Linux by-id/by-path symlinks
                )
                for port_info in list_ports.comports():
                    port_path = port_info.device
                    if (port_path.startswith(accepted_prefixes)
                            or re.fullmatch(r'COM\d+', port_path, re.IGNORECASE)
                            or re.fullmatch(r'\\\\\.\\COM\d+', port_path, re.IGNORECASE)):
                        if port_path not in seen:
                            seen.add(port_path)
                            desc = port_info.description or 'Serial Port'
                            found.append({'port': port_path, 'description': desc})
            except ImportError:
                pass

        return found

    def connect(self, port: str, baud: int = DEFAULT_BAUD) -> bool:
        """Connect to the specified serial port.

        Args:
            port: Serial port path (e.g. /dev/tty.wchusbserial1420)
            baud: Baud rate (default 115200)

        Returns:
            True if connection successful.
        """
        with self._lock:
            if self._serial and self._serial.is_open:
                self._serial.close()

            try:
                self._serial = serial.Serial(
                    port=port,
                    baudrate=baud,
                    bytesize=DEFAULT_BYTESIZE,
                    parity=DEFAULT_PARITY,
                    stopbits=DEFAULT_STOPBITS,
                    timeout=DEFAULT_TIMEOUT,
                    write_timeout=DEFAULT_TIMEOUT,
                )
                self._port = port
                self._baud = baud
                self._connected = True
                logger.info(f'Connected to {port} at {baud} baud')
                return True
            except serial.SerialException as e:
                logger.error(f'Failed to connect to {port}: {e}')
                self._serial = None
                self._connected = False
                return False

    def disconnect(self):
        """Disconnect from the serial port."""
        with self._lock:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.close()
                except Exception as e:
                    logger.warning(f'Error closing port: {e}')
            self._serial = None
            self._connected = False
            self._port = ''
            logger.info('Disconnected')

    def wakeup(self, port: str) -> dict:
        """Send the 1200 baud wakeup sequence then reconnect at 115200.

        This follows the procedure from working implementations:
        1. Open port at 1200 baud
        2. Send the wakeup frame
        3. CLOSE the port completely
        4. Reopen at 115200 baud
        5. Send CR+LF to trigger the pylon> prompt
        6. Check for response

        Args:
            port: Serial port path.

        Returns:
            Dict with 'success', 'prompt_found', 'response' keys.
        """
        result = {'success': False, 'prompt_found': False, 'response': ''}

        # Phase 1: Send wakeup at 1200 baud
        logger.info(f'Wakeup phase 1: sending wakeup frame at 1200 baud on {port}')
        try:
            ser = serial.Serial(
                port=port,
                baudrate=1200,
                bytesize=DEFAULT_BYTESIZE,
                parity=DEFAULT_PARITY,
                stopbits=DEFAULT_STOPBITS,
                timeout=2.0,
                write_timeout=2.0,
            )
            ser.reset_input_buffer()
            ser.write(WAKEUP_FRAME)
            ser.flush()
            time.sleep(1.0)  # Wait for battery to process

            # Read any response at 1200 baud
            if ser.in_waiting > 0:
                resp_1200 = ser.read(ser.in_waiting)
                logger.info(f'Wakeup 1200 baud response: {resp_1200.hex()} ({len(resp_1200)} bytes)')
            else:
                logger.info('No response at 1200 baud (normal for some batteries)')

            ser.close()
            logger.info('Wakeup phase 1 complete, port closed')
        except serial.SerialException as e:
            logger.error(f'Wakeup phase 1 failed: {e}')
            result['response'] = f'Failed to open port at 1200 baud: {e}'
            return result

        time.sleep(0.5)  # Brief pause before reopening

        # Phase 2: Reopen at 115200 and send CR+LF to get prompt
        logger.info('Wakeup phase 2: opening at 115200 baud')
        try:
            ser = serial.Serial(
                port=port,
                baudrate=115200,
                bytesize=DEFAULT_BYTESIZE,
                parity=DEFAULT_PARITY,
                stopbits=DEFAULT_STOPBITS,
                timeout=2.0,
                write_timeout=2.0,
            )
        except serial.SerialException as e:
            logger.error(f'Wakeup phase 2 failed to open: {e}')
            result['response'] = f'Failed to reopen at 115200: {e}'
            return result

        # Try sending CR+LF up to 5 times, checking for pylon> prompt
        all_responses = []
        for attempt in range(5):
            logger.info(f'Wakeup: sending CR+LF (attempt {attempt + 1}/5)')
            ser.reset_input_buffer()
            ser.write(b'\r\n')
            ser.flush()
            time.sleep(1.0)

            response = b''
            if ser.in_waiting > 0:
                response = ser.read(ser.in_waiting)
                text = response.decode('ascii', errors='replace')
                all_responses.append(text)
                logger.info(f'Wakeup response: {repr(text)}')

                if 'pylon' in text.lower() or '>' in text:
                    logger.info('Found pylon> prompt!')
                    result['prompt_found'] = True
                    result['success'] = True
                    # Store this serial connection as our active one
                    with self._lock:
                        if self._serial and self._serial.is_open:
                            self._serial.close()
                        self._serial = ser
                        self._port = port
                        self._baud = 115200
                        self._connected = True
                    result['response'] = text
                    return result
            else:
                all_responses.append('(no response)')
                logger.info(f'No response on attempt {attempt + 1}')

        # No prompt found, but keep the connection open anyway
        # (battery might still respond to commands)
        result['response'] = ' | '.join(all_responses)
        result['success'] = True  # Connection is open even if no prompt
        with self._lock:
            if self._serial and self._serial.is_open:
                self._serial.close()
            self._serial = ser
            self._port = port
            self._baud = 115200
            self._connected = True
        logger.info('Wakeup complete (no prompt found, but connection kept open)')
        return result

    def send(self, data: bytes) -> bool:
        """Send raw bytes to the serial port.

        Args:
            data: Bytes to send.

        Returns:
            True if send successful.
        """
        with self._lock:
            if not self.is_connected:
                return False
            try:
                self._serial.write(data)
                self._serial.flush()
                return True
            except serial.SerialException as e:
                logger.error(f'Send error: {e}')
                self._connected = False
                return False

    def receive(self, size: int = 4096, timeout: float | None = None) -> bytes:
        """Read bytes from the serial port.

        Args:
            size: Maximum bytes to read.
            timeout: Override read timeout (seconds).

        Returns:
            Received bytes (may be empty on timeout).
        """
        with self._lock:
            if not self.is_connected:
                return b''
            try:
                old_timeout = self._serial.timeout
                if timeout is not None:
                    self._serial.timeout = timeout
                data = self._serial.read(size)
                if timeout is not None:
                    self._serial.timeout = old_timeout
                return data
            except serial.SerialException as e:
                logger.error(f'Receive error: {e}')
                self._connected = False
                return b''

    def receive_until(self, terminator: bytes = b'\r', timeout: float = 3.0) -> bytes:
        """Read bytes until a terminator character is received.

        Args:
            terminator: Byte(s) that signal end of response.
            timeout: Maximum time to wait (seconds).

        Returns:
            Received bytes including terminator (may be empty on timeout).
        """
        with self._lock:
            if not self.is_connected:
                return b''
            try:
                result = bytearray()
                start = time.time()
                while (time.time() - start) < timeout:
                    if self._serial.in_waiting > 0:
                        byte = self._serial.read(1)
                        if byte:
                            result.extend(byte)
                            if result.endswith(terminator):
                                return bytes(result)
                    else:
                        time.sleep(0.01)
                return bytes(result)
            except serial.SerialException as e:
                logger.error(f'Receive error: {e}')
                self._connected = False
                return b''

    def send_command(self, command: str, timeout: float = 3.0) -> str:
        """Send a text command and receive the text response.

        Used for console CLI mode (115200 baud text commands).

        Args:
            command: Text command to send (without trailing newline).
            timeout: Response timeout in seconds.

        Returns:
            Response text (decoded).
        """
        with self._lock:
            if not self.is_connected:
                return ''
            try:
                # Clear any pending input
                self._serial.reset_input_buffer()

                # Send command with carriage return
                cmd_bytes = (command + '\r').encode('ascii')
                self._serial.write(cmd_bytes)
                self._serial.flush()

                # Read response - console commands return multiple lines
                # terminated by the command prompt
                response = bytearray()
                start = time.time()
                idle_start = None

                while (time.time() - start) < timeout:
                    if self._serial.in_waiting > 0:
                        chunk = self._serial.read(self._serial.in_waiting)
                        response.extend(chunk)
                        idle_start = None
                    else:
                        if idle_start is None:
                            idle_start = time.time()
                        elif (time.time() - idle_start) > 0.5:
                            # No data for 500ms after receiving some data = done
                            if len(response) > 0:
                                break
                        time.sleep(0.01)

                text = response.decode('ascii', errors='replace')
                # Strip RS-485 echo of the command we sent
                cmd_echo = command + '\r'
                if text.startswith(cmd_echo):
                    text = text[len(cmd_echo):]
                return text
            except serial.SerialException as e:
                logger.error(f'Command error: {e}')
                self._connected = False
                return ''

    def send_binary(self, frame: bytes, timeout: float = 3.0) -> bytes:
        """Send a binary protocol frame and receive the response frame.

        Pylon binary frames start with 0x7E (~) and end with 0x0D (CR).
        RS-485 is half-duplex so we read back our own echo first, then
        the battery's response.

        Args:
            frame: Complete binary frame to send.
            timeout: Response timeout in seconds.

        Returns:
            Response frame bytes (excluding the echo).
        """
        with self._lock:
            if not self.is_connected:
                return b''
            try:
                self._serial.reset_input_buffer()
                self._serial.write(frame)
                self._serial.flush()

                start = time.time()
                frames_found = []
                current_frame = bytearray()
                in_frame = False

                while (time.time() - start) < timeout:
                    if self._serial.in_waiting > 0:
                        byte = self._serial.read(1)
                        if byte == b'\x7e' or byte == b'~':
                            # Start of a new frame
                            in_frame = True
                            current_frame = bytearray(byte)
                        elif in_frame:
                            current_frame.extend(byte)
                            if byte == b'\r' or byte == b'\x0d':
                                frames_found.append(bytes(current_frame))
                                in_frame = False
                                # First frame is our echo, second is the response
                                if len(frames_found) >= 2:
                                    return frames_found[1]
                    else:
                        # If we already got the echo and there's nothing more, wait a bit
                        if len(frames_found) >= 1:
                            # Give the battery some time to respond after echo
                            time.sleep(0.05)
                            if self._serial.in_waiting == 0:
                                # Short extra wait then check again
                                time.sleep(0.2)
                                if self._serial.in_waiting == 0:
                                    # No response after echo - battery didn't reply
                                    logger.debug('Only echo received, no battery response')
                                    return b''
                        else:
                            time.sleep(0.01)

                # Timeout - return whatever we have
                if len(frames_found) >= 2:
                    return frames_found[1]
                logger.debug(f'Timeout: got {len(frames_found)} frame(s)')
                return b''
            except serial.SerialException as e:
                logger.error(f'Binary command error: {e}')
                self._connected = False
                return b''
