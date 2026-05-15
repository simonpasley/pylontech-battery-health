# Hardware setup

What you need, where to plug in, and how to identify which pack is which.

> **Disclaimer**: This is an unofficial community tool. Working on lithium battery systems is dangerous. If you are not competent and qualified, **do not** open the rack, plug or unplug cables, or run diagnostic commands. The authors accept zero liability — see the project [LICENSE](../LICENSE).

---

## 1. The cable

You need a USB-to-RS232 cable terminating in an **RJ45 (8P8C)** connector wired for the Pylontech console pinout.

### Verified working

| Item | Detail |
|---|---|
| Cable | `uamdoen` US2000C / US3000C / US5000 Lithium Battery BMS Console Communication Cable |
| Chipset | FTDI **FT231XS** |
| Connectors | USB-A to RJ45 8P8C |
| Length | 6 ft / 180 cm |
| Example listing | [amzn.eu/d/02uypFNx](https://amzn.eu/d/02uypFNx) — verify the chipset before buying; Amazon listings change |

Other Pylontech-pinout cables work — anything sold as a "Pylontech BMS console cable" with an FTDI / CH340 / CP210x chipset.

### Drivers

- **macOS 10.15+ / Linux**: built-in, plug and play
- **Windows 10/11**: usually built-in. If the device shows up as "Unknown" in Device Manager, install [FTDI VCP drivers](https://ftdichip.com/drivers/vcp-drivers/) for FT231XS, or [CH340 drivers](http://www.wch-ic.com/downloads/CH341SER_EXE.html) if your cable has that chipset.

### Identifying the port name

| OS | Likely port name | Where to find it |
|---|---|---|
| macOS | `/dev/cu.usbserial-XXXXXXXX` | The toolkit lists it automatically — pick from the dropdown |
| Linux | `/dev/ttyUSB0` (or similar) | `dmesg \| tail` after plugging in |
| Windows | `COM3`, `COM4` etc | Device Manager → Ports (COM & LPT) |

The toolkit auto-prefers `cu.*` over `tty.*` on macOS — `tty.*` waits for a DCD signal that Pylontech batteries do not provide, which causes read hangs.

---

## 2. The ports on a Pylontech master

A Pylontech master module has several RJ45 ports. **Use the console / RS232 port — not anything else.**

| Port label | Purpose | Use with this toolkit? |
|---|---|---|
| **Console / RS232** | Plain text BMS shell | ✓ **YES — this is what you want** |
| RS485 | Inter-pack stacking + some inverter integrations | ✗ no (different protocol) |
| CAN | Inverter integration | ✗ no — will return garbled characters |
| Link-port-1 / Link-port-2 / Link-up / Link-down | Inter-pack daisy chain | ✗ no |
| Dry-contact / dry-input | Alarm relay outputs | ✗ no (not a comms port) |

If you are unsure which port is the console port, consult the relevant **Pylontech User Manual** for your model:

- US3000C / US5000 user manuals are available from your installer or by request from Pylontech / your distributor.
- The console port is usually labelled **Console** or **RS232** on the case silkscreen.

If the toolkit's wakeup returns the `pylon>` prompt: you're on the right port. If it returns garbled `|` characters or nothing at all, you're on the wrong port.

---

## 3. Master vs slave: when to use which

Pylontech racks consist of one **master** module and 0–N **slave** modules connected via inter-pack link cables. Address 1 is the master; addresses 2 onwards are slaves.

### Plug into the master to:

- See the whole-rack view (`pwr` returns a multi-pack table — voltage / current / SOC / cell-spread for every online pack)
- Identify which packs have problems (look for spreads > 30 mV)
- Run `info N`, `pwr N`, `bat N`, `soh N` against any slave by address (these queries propagate via the comm bus)

### Plug into an individual pack to:

- Get the **full event log** (`log`) for that pack — this is local-only and does **not** propagate via the master
- Get authoritative per-cell **SOH-abnormal counts** (`soh N` against a slave often returns 0 for all SOHCount fields when relayed via the master, even when the slave's local query shows non-zero counts)
- Capture warranty-grade detail on a known-failed pack

### Identifying the master physically

The master is usually:
- The pack with **DIP switches set to `0000`** (or the lowest address — Pylontech convention varies)
- The pack with the **inverter CAN cable plugged into its CAN port**
- The pack at the **top or bottom** of the stack, depending on rack design

The toolkit will tell you which address you're connected to via the `info` command — that's the authoritative answer once you're plugged in.

---

## 4. Cable plug/unplug order

Pylontech batteries are designed for hot-pluggable RS232 console connection. However, to minimise any risk:

1. **Pause your inverter** if you can do so non-disruptively (Victron Cerbo / GX UI → Battery → disable, or shut the inverter to standby)
2. **Plug the cable into the battery first**, then into the computer — avoids ESD into a powered USB host
3. To move the cable to another pack: unplug from the *current* pack first, then plug into the *next* pack
4. **Never** plug the console cable into a CAN port that is actively talking to an inverter — the wrong protocol on the wrong port can confuse both ends

These are conservative practices. Pylontech batteries with healthy BMS firmware tolerate live console plugging, but lithium installs deserve respect.

---

## 5. Common cable problems

| Symptom | Cause | Fix |
|---|---|---|
| Cable not in port dropdown on macOS | Driver not loaded / cable not plugged in | Re-plug, click the ↻ button to refresh, check `ls /dev/cu.*` |
| Garbled `|` characters from wakeup | Plugged into the wrong port (CAN, RS485, link) | Move cable to console / RS232 port |
| No response at all from wakeup | Console asleep — wakeup didn't take | Try a second time. If still no response, swap the cable end-for-end (some cables only work in one orientation) |
| Connect succeeds but `info` returns gibberish | Wrong baud rate / cable damaged | Default 115200 should be correct. Try a different cable |
| Connect drops after a few seconds | Loose RJ45 contact | Reseat the cable in both the battery and the USB end |

---

## 6. What you do **not** need

- A Cerbo GX or other Victron device — the toolkit talks directly to the battery
- BatteryView or any Pylontech Windows software — same reason
- An internet connection — fully local
- An inverter to be running — the BMS responds whether the inverter is on or not (though pause the inverter if you want a clean idle reading)
