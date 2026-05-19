# Understanding your results

You've run a health check — here's how to read the verdict and what sensible next steps look like.

> **Disclaimer**: This is an unofficial community tool. It reads what the BMS reports about itself; it is not an official assessment and not legal or contractual advice. Always speak to your installer or Pylontech through their official channels about anything you're unsure of. The author accepts zero liability — see the project [LICENSE](../LICENSE).

---

## What the verdicts mean

| Verdict | Meaning |
|---|---|
| **HEALTHY** | Cell-voltage spread and SOH indicators are within normal range under load. Nothing to act on. |
| **DEGRADING** | Early imbalance / SOH-abnormal signs. Worth keeping an eye on — re-check periodically and note the trend. |
| **FAILED** | Clear failure signature (large cell-voltage spread, BMS-reported SOH at end-of-life, or a runtime abnormal flag). |
| **UNKNOWN** | Measurement conditions weren't valid (idle pack, extreme SOC, cold). Re-test under light load at moderate SOC — the tool deliberately withholds a verdict rather than risk a false result. |

The verdict logic is intentionally **conservative**. If a pack is genuinely fine it will read HEALTHY; if conditions aren't right to judge, it returns UNKNOWN rather than guessing. A FAILED or DEGRADING result reflects what the pack's own BMS is reporting about itself.

---

## Sensible next steps

1. **Re-run under load.** Cell-spread thresholds are only meaningful when the pack is doing something (≥ ~0.2 A, moderate SOC, not cold). If you got UNKNOWN, test again while the rack is charging or discharging.
2. **Capture the full detail for any flagged pack.** Plug the cable directly into that pack's console port and re-run, so you get its authoritative per-cell SOH counts and complete event log (these don't fully propagate via the master).
3. **Inspect the pack physically.** This tool reads electrical data only — it can't see swelling, blue residue at the ports, corroded contacts or heat damage. Always look at a flagged pack as well.
4. **If a pack looks faulty, talk to the right people.** Your installer, or Pylontech via their official support channels, are the people who advise on what happens next. The report this tool produces simply shows the battery's own BMS data, unaltered — it's there for your records and to make that conversation an informed one. This tool doesn't make any claim on your behalf or tell you whether a pack is in or out of warranty.

---

## Storing a pack you've taken out of service

If you remove a suspect pack from a rack while you sort out next steps, treat it with respect — a failing cell deserves care:

- **Moderate state of charge** (~50 %) — not full, not empty
- **Disconnected** from any inverter
- **Cool, dry, ventilated** — not in sunlight, damp, a car boot, or an airless corner
- **Away from flammable materials**

LFP is the safer lithium chemistry, but a failed pack still warrants the same careful storage as any lithium battery.

---

## What this tool does not do

- Negotiate with Pylontech, make any claim, or guarantee any outcome
- Replace any official Pylontech tooling or process — if an official workflow expects BatteryView's exact export, use that as well
- Tell you whether a pack is in or out of warranty — that's between you, your installer and Pylontech
- Diagnose physical damage — always inspect the pack as well

---

## Honest summary

This tool reads what the BMS says about itself and explains it in plain English. It won't magic anything out of nothing — but if a pack is genuinely degrading, an honest, readable picture of its own data is a good place to start an informed conversation with the people who can help.
