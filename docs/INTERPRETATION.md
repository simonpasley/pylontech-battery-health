# Interpreting Pylontech BMS data

What every metric means, what's normal, and what flags a failure.

> **Disclaimer**: This guide is offered as informational context only — a health-check aid, not an official assessment. The author accepts zero liability for any decision made from this guide. If a pack looks faulty, your installer or Pylontech are the right people to advise. See the project [LICENSE](../LICENSE).

---

## The headline indicator: cell-voltage spread

In any series-connected lithium pack, all cells carry the same current. So at any moment, each cell's voltage reflects its individual state-of-charge **and** its internal resistance. In a healthy pack, all cells track each other very closely — a few millivolts apart.

When a cell starts to fail (capacity loss, increased internal resistance, or both), it deviates from its siblings under load.

### Thresholds (under moderate load, 1–5 A)

| Spread | Meaning | Toolkit verdict |
|---|---|---|
| < 30 mV | Healthy — normal cell-to-cell variance | HEALTHY |
| 30–50 mV | Emerging imbalance — worth monitoring | DEGRADING |
| > 50 mV | At least one cell has a fault — IR rise, capacity loss, or both | FAILED |

Pylontech US3000C / US5000 use 15-cell LFP series strings. Each cell sits around **3.30–3.32 V** at mid-SOC under light load. Healthy spreads are typically **1–5 mV**. A 100 mV spread means one cell is 100 mV away from the rest, which is a strong sign of a failed cell.

> **Important caveat**: at very low or very high SOC, the LFP voltage curve steepens dramatically, so spreads above 30 mV are normal at the very ends of charge / discharge. Best to read spread when the pack is between 20 % and 80 % SOC under light-to-moderate current.

---

## The other definitive indicator: BMS-reported SOH

The Pylontech BMS computes its own State of Health internally and exposes it via the `stat` command's **SOH** field (a percentage).

| `stat → SOH` | Meaning |
|---|---|
| 99–100 | Normal |
| 70–98 | Degraded but functional |
| 1–69 | Significant capacity loss |
| **0** | **The BMS has declared the pack end-of-life — the strongest single end-of-life signal.** |

The toolkit treats `SOH = 0` as forcing a FAILED verdict regardless of any other metric, because the BMS itself has spoken.

---

## The runtime alarm: `pwr → Soh. Status`

The `pwr` command shows a real-time `Soh. Status` field that indicates whether the BMS is currently flagging an SOH alarm:

- `Normal` — no current alarm
- `Abnormal` — alarm is active right now

The toolkit forces FAILED when this is `Abnormal`.

Note: this flag can come and go depending on operating conditions, while `stat → SOH` is a more stable lifetime metric.

---

## Per-cell SOH counters

The `soh N` command lists each cell with two columns:

- **`SOHCount`** — number of times the BMS has flagged that cell as "abnormal" over the lifetime of the pack
- **`SOHStatus`** — the cell's current status flag (`Normal` / `Abnormal`)

Any cell with `SOHCount` > 0 has been flagged at some point. Counts in the hundreds indicate sustained, repeated abnormal events on that specific cell — a clear localised failure indicator.

> **Comm-bus limitation**: when `soh N` is queried via the master pack against a slave pack, SOHCount values often come back as 0 even when the slave's own local query shows non-zero counts. The pack-level `Soh. Status: Abnormal` does propagate. To get authoritative per-cell SOHCount data, plug the cable directly into the suspect pack's console port.

---

## Real cycle count vs charge transitions

The `stat` command exposes two cycle-related numbers and they are **not** the same thing:

| Field | What it actually counts |
|---|---|
| **`CYCLE Times`** | True full-equivalent charge cycles. **This is the meaningful real-cycle metric.** |
| `Charge Times` | A transition counter (BMS state changes that include charge events). Often an order of magnitude higher than the real cycle count. **NOT a real cycle count — don't mistake it for one.** |

A US3000C is rated for **6 000+ cycles** to 80 % capacity under nominal conditions. A pack failing at < 1 000 real cycles is failing well short of rated life.

---

## Per-cell capacity tracking (`data event`)

The `data event` command shows the most recent stored event with a per-cell snapshot. A column called **Coulomb** shows each cell's tracked capacity as a percentage of nominal:

| Cell coulomb % | Meaning |
|---|---|
| 90–95 % | Normal mid-life cell |
| 70–89 % | Cell is losing capacity |
| < 70 % | Significant capacity loss — this cell is failing |

A cell can have **normal voltage** at idle but a **low coulomb %** — meaning its capacity has shrunk, but at any given SOC its voltage still looks fine. These cells will fail "high" (over-voltage) at the top of charge before any voltage spread is visible at idle.

This is why looking at voltage spread alone is not sufficient — a pack can have one cell sagging under load (voltage indicator) **and** a different cell with capacity loss (coulomb indicator) at the same time. The toolkit shows both.

---

## Lifetime alarm trip counts

| `stat` field | Meaning | What non-zero values indicate |
|---|---|---|
| `Bat OV Times` | Cell over-voltage protection trips | High — top cell hit ~3.60 V; usually due to imbalance at end of charge |
| `Bat HV Times` | Cell high-voltage warnings | Less critical than OV but same root cause |
| `Bat LV Times` | Cell low-voltage warnings | Bottom cell sagging at end of discharge |
| `Bat UV Times` | Cell under-voltage protection trips | Same as LV but at the harder cutoff threshold |
| `COC / DOC Times` | Charge / discharge over-current trips | **Abuse indicator** — if non-zero, possibly mis-sized inverter or fault current event |
| `SC Times` | Short-circuit trips | **Abuse indicator** — should be 0 on a healthy install |
| `Shut Times` | Pack shutdowns | High counts → BMS frequently throwing in the towel |
| `Reset Times` | BMS resets | High counts → instability |

For a health assessment, **abuse indicators (COC, DOC, SC) being all 0** is good news — it shows the pack was not over-stressed. Voltage-only trips (OV, HV, LV, UV) are not abuse, they are the BMS protecting itself from imbalanced cells.

---

## Putting it together — failure signatures

### Classic high-IR cell

- One cell sags **under load** (voltage spread > 50 mV during discharge)
- That cell **recovers** towards the pack mean at idle
- BMS triggers BLV (low-voltage) alarms during heavy discharge
- Voltage spread visible in `bat N`

### Classic capacity-loss cell

- Cell looks **fine on voltage** — within a few mV of pack mean at all SOCs
- `data event` Coulomb column shows that cell at a much lower percentage
- BMS triggers BHV / BOV at end of charge as that cell fills first
- Sometimes shows up via SOHCount creeping up over months

### A failing pack typically has both

Most failing Pylontech packs we've examined have at least one cell of each type — a high-IR cell pinching the bottom of the curve and a capacity-loss cell pinching the top. Hence the toolkit checks both `bat` and `data event` and includes both in the health report.

### Batch defects

When you see multiple packs from the same production batch (sequential serial numbers, identical release date) showing the same failure mode in the same rack, that is a strong signal of a batch issue rather than independent end-of-life. Worth noting that pattern if you discuss the rack with your installer or Pylontech.
