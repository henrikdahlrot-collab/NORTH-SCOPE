#!/usr/bin/env python3
"""Enhetstester för server/port_calls.py och server/ports.py (utan databas).

    python3 tests/test_port_calls.py
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import port_calls as pc  # noqa: E402
import ports  # noqa: E402

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
P = pc.Params()


def t(minutes):
    return T0 + timedelta(minutes=minutes)


def row(minutes, sog, loc, nav=0):
    return (t(minutes), sog, nav, loc)


class Coordinates(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ports.parse_coordinates("5743N 01158E"), (57.71667, 11.96667))
        self.assertEqual(ports.parse_coordinates("3351S 01825W"), (-33.85, -18.41667))
        for bad in ("", None, "5743 01158E", "5799N 01158E", "9100N 01158E", "5743N 18160E"):
            self.assertIsNone(ports.parse_coordinates(bad), bad)


class Detect(unittest.TestCase):
    def test_normal_call(self):
        rows = [row(0, 10, None), row(10, 5, "A"), row(60, 0.2, "A"), row(120, 0.2, "A"),
                row(130, 6, "A"), row(140, 10, None)]
        (v,) = pc.detect_calls(rows, True, P)
        self.assertEqual((v["locode"], v["arrived"], v["departed"]), ("A", t(10), t(140)))
        self.assertTrue(pc.qualifies(v, P))
        self.assertTrue(v["arrival_known"] and v["departure_known"] and not v["open"])

    def test_pass_through_does_not_qualify(self):
        rows = [row(0, 12, None), row(5, 12, "A"), row(25, 12, "A"), row(30, 12, None)]
        (v,) = pc.detect_calls(rows, True, P)
        self.assertFalse(pc.qualifies(v, P))

    def test_anchored_does_not_qualify(self):
        rows = [row(0, 10, None), row(10, 0.1, "A", nav=1), row(400, 0.1, "A", nav=1), row(410, 10, None)]
        (v,) = pc.detect_calls(rows, True, P)
        self.assertFalse(pc.qualifies(v, P))

    def test_short_stop_does_not_qualify(self):
        rows = [row(0, 10, None), row(10, 0.2, "A"), row(20, 0.2, "A"), row(30, 10, None)]
        (v,) = pc.detect_calls(rows, True, P)
        self.assertFalse(pc.qualifies(v, P))

    def test_unknown_arrival_when_first_ever_position_is_inside(self):
        rows = [row(0, 0.2, "A"), row(100, 0.2, "A"), row(110, 10, None)]
        (v,) = pc.detect_calls(rows, False, P)
        self.assertFalse(v["arrival_known"])
        self.assertFalse(v["truncated_start"])

    def test_truncated_start_when_history_exists(self):
        rows = [row(0, 0.2, "A"), row(100, 0.2, "A")]
        (v,) = pc.detect_calls(rows, True, P)
        self.assertTrue(v["truncated_start"])
        self.assertTrue(v["open"])
        self.assertIsNone(v["departed"])

    def test_edge_jitter_is_merged(self):
        rows = [row(0, 10, None), row(10, 3, "A"), row(60, 0.2, "A"), row(70, 1, None),
                row(80, 1, None), row(90, 0.5, "A"), row(150, 0.2, "A"), row(160, 8, None)]
        calls = pc.detect_calls(rows, True, P)
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["arrived"], calls[0]["departed"]), (t(10), t(160)))

    def test_return_after_long_time_is_a_new_call(self):
        rows = [row(0, 10, None), row(10, 0.2, "A"), row(60, 0.2, "A"), row(70, 10, None),
                row(400, 10, None), row(410, 0.2, "A"), row(470, 0.2, "A"), row(480, 10, None)]
        self.assertEqual(len(pc.detect_calls(rows, True, P)), 2)

    def test_two_ports_in_a_row_are_not_merged(self):
        rows = [row(0, 10, None), row(10, 0.2, "A"), row(60, 0.2, "A"), row(70, 5, "B"),
                row(120, 0.2, "B"), row(180, 0.2, "B"), row(190, 10, None)]
        calls = pc.detect_calls(rows, True, P)
        self.assertEqual([c["locode"] for c in calls], ["A", "B"])
        self.assertEqual(calls[0]["departed"], t(70))

    def test_departure_unknown_after_long_gap(self):
        rows = [row(0, 10, None), row(10, 0.2, "A"), row(60, 0.2, "A"), row(60 + 7 * 60, 10, None)]
        (v,) = pc.detect_calls(rows, True, P)
        self.assertFalse(v["departure_known"])
        self.assertEqual(v["departed"], t(60))

    def test_nothing_in_ports(self):
        self.assertEqual(pc.detect_calls([row(0, 10, None), row(10, 10, None)], True, P), [])
        self.assertEqual(pc.detect_calls([], True, P), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
