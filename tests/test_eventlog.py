"""Golden-file tests for the event-log parser/summariser.

Fixtures are two real `log` dumps (serial numbers redacted) captured from
US3000C packs on B69-series firmware:

  * eventlog-faulty.txt — a pack actively failing: under-voltage protection
    trips plus repeated BMS shutdown/restart.
  * eventlog-clean.txt  — a pack doing nothing but normal charge/discharge
    cycling.

These lock in the behaviour that matters — faults surface, routine noise
doesn't — so a future parser tweak for another model can't silently regress.

Run: python -m unittest discover -s tests
"""

import os
import unittest

from pylontech.eventlog import (
    CRITICAL,
    INFO,
    NOTABLE,
    parse_event_log,
    render_history_filehead,
    render_summary_filehead,
    summarise_event_history,
    summarise_event_log,
    summary_to_dict,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class TestParse(unittest.TestCase):
    def test_parses_all_32_entries(self):
        for fixture in ("eventlog-faulty.txt", "eventlog-clean.txt"):
            events = parse_event_log(_load(fixture))
            self.assertEqual(len(events), 32, fixture)

    def test_ignores_header_and_paging_noise(self):
        # No event should be created from "Press [Enter]…" prompts or the
        # leading "# ..." comment header.
        events = parse_event_log(_load("eventlog-faulty.txt"))
        for e in events:
            self.assertNotIn("Press", e.info)
            self.assertTrue(e.info)

    def test_timestamps_parsed(self):
        events = parse_event_log(_load("eventlog-clean.txt"))
        self.assertTrue(all(e.timestamp is not None for e in events))


class TestFaultyPack(unittest.TestCase):
    def setUp(self):
        self.s = summarise_event_log(_load("eventlog-faulty.txt"))

    def test_overall_severity_critical(self):
        self.assertEqual(self.s.severity, CRITICAL)

    def test_counts_undervoltage_restart_shutdown(self):
        self.assertEqual(self.s.counts.get("undervoltage"), 2)
        self.assertEqual(self.s.counts.get("restart"), 2)
        self.assertEqual(self.s.counts.get("shutdown"), 2)

    def test_headline_flags_faults(self):
        self.assertIn("FAULTS LOGGED", self.s.headline)
        self.assertIn("power-cycled itself", self.s.headline)

    def test_critical_events_captured(self):
        self.assertEqual(len(self.s.critical), 2)
        self.assertTrue(all("Under-voltage" in e.label for e in self.s.critical))
        # Codes from the real log.
        self.assertEqual({e.code for e in self.s.critical}, {"72c", "72d"})

    def test_filehead_leads_with_fault(self):
        head = render_summary_filehead(self.s)
        self.assertIn("[FAULT]", head)
        self.assertTrue(head.startswith("# === PLAIN-ENGLISH SUMMARY"))


class TestCleanPack(unittest.TestCase):
    def setUp(self):
        self.s = summarise_event_log(_load("eventlog-clean.txt"))

    def test_overall_severity_info(self):
        self.assertEqual(self.s.severity, INFO)

    def test_no_fault_categories(self):
        for bad in ("undervoltage", "overvoltage", "restart", "shutdown"):
            self.assertEqual(self.s.counts.get(bad, 0), 0, bad)

    def test_headline_is_clean(self):
        self.assertIn("Clean", self.s.headline)
        self.assertEqual(self.s.critical, [])

    def test_only_cycling_logged(self):
        self.assertGreater(self.s.counts.get("charge", 0), 0)
        self.assertGreater(self.s.counts.get("discharge", 0), 0)


class TestEmpty(unittest.TestCase):
    def test_empty_input_is_safe(self):
        s = summarise_event_log("")
        self.assertEqual(s.events, [])
        self.assertIn("No readable events", s.headline)

    def test_summary_dict_is_serialisable(self):
        import json
        s = summarise_event_log(_load("eventlog-faulty.txt"))
        json.dumps(summary_to_dict(s))  # must not raise


_HISTORY_SAMPLE = """\
# === data event history (full) ===
-----------------------------------------------
Item Index      : 100
Time            : 26-08-23 21:07:59
Voltage         : 51807       mV
Current         : 5000        mA
Temperature     : 26300       mC
Percent         : 99          %
Max Voltage     : 54000       mV
Base State      : Charge
Volt. State     : Normal
Bat Events      : 0x8        BHV
System Fault    : 0x0
-----------------------------------------------

Battery  Volt     Curr     Tempr    Base State   Volt. State  Curr. State  Temp. State  Coulomb
0        3450     5000     26000    Charge       Normal       Normal       Normal       99%
1        3650     5000     26000    Charge       High         Normal       Normal       99%
2        3455     5000     26000    Charge       Normal       Normal       Normal       99%
-----------------------------------------------
Item Index      : 50
Time            : 26-08-21 01:51:11
Voltage         : 46000       mV
Current         : -9000       mA
Temperature     : 25000       mC
Percent         : 2           %
Base State      : Dischg
Volt. State     : Normal
Bat Events      : 0x10       BUV
System Fault    : 0x0
-----------------------------------------------

Battery  Volt     Curr     Tempr    Base State   Volt. State  Curr. State  Temp. State  Coulomb
0        3100     -9000    25000    Dischg       Normal       Normal       Normal       2%
1        2400     -9000    25000    Dischg       UV           Normal       Normal       2%
2        3090     -9000    25000    Dischg       Normal       Normal       Normal       2%
"""


class TestEventHistory(unittest.TestCase):
    def setUp(self):
        self.h = summarise_event_history(_HISTORY_SAMPLE)

    def test_counts_items_and_span(self):
        self.assertEqual(self.h.item_count, 2)
        self.assertIsNotNone(self.h.span_start)

    def test_finds_peak_and_lowest_cell(self):
        self.assertEqual(self.h.max_cell_mv, 3650)
        self.assertEqual(self.h.max_cell_num, 1)
        self.assertEqual(self.h.min_cell_mv, 2400)
        self.assertEqual(self.h.min_cell_num, 1)   # same failing cell swings both ways

    def test_bat_event_flags_tallied(self):
        self.assertEqual(self.h.bat_events.get("BHV"), 1)
        self.assertEqual(self.h.bat_events.get("BUV"), 1)

    def test_severity_and_headline(self):
        # BUV present -> critical; peak cell is into high-voltage.
        self.assertEqual(self.h.severity, CRITICAL)
        self.assertIn("3650 mV", render_history_filehead(self.h))
        self.assertIn("high/over-voltage", render_history_filehead(self.h))

    def test_clean_history(self):
        clean = _HISTORY_SAMPLE.replace("BHV", "").replace("BUV", "") \
            .replace("High ", "Normal").replace("UV   ", "Normal") \
            .replace("2400", "3300")
        h = summarise_event_history(clean)
        self.assertEqual(h.severity, INFO)


if __name__ == "__main__":
    unittest.main()
