#!/usr/bin/env python3
"""Enhetstester för server/live_collector.py (utan databas och utan nätverk).

    python3 tests/test_live_collector.py
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import live_collector as lc  # noqa: E402

MIDS = lc.load_mid_map(ROOT / "server" / "data" / "mid_countries.csv")


def pos_msg(mmsi, lat, lon, sog=10.0, cog=90.0, kind="PositionReport"):
    return {"MessageType": kind, "MetaData": {"MMSI": int(mmsi)},
            "Message": {kind: {"UserID": int(mmsi), "Latitude": lat, "Longitude": lon,
                               "Sog": sog, "Cog": cog, "NavigationalStatus": 0}}}


def static_msg(mmsi, ship_type, length=100):
    return {"MessageType": "ShipStaticData", "MetaData": {"MMSI": int(mmsi)},
            "Message": {"ShipStaticData": {"UserID": int(mmsi), "Type": ship_type,
                                           "Dimension": {"A": length - 10, "B": 10, "C": 4, "D": 4}}}}


class MidTable(unittest.TestCase):
    def test_nordic_and_other_flags(self):
        for mid, iso in (("265", "SE"), ("266", "SE"), ("257", "NO"), ("258", "NO"), ("259", "NO"),
                         ("219", "DK"), ("220", "DK"), ("230", "FI"), ("251", "IS"),
                         ("636", "LR"), ("538", "MH"), ("351", "PA"), ("273", "RU"), ("211", "DE")):
            self.assertEqual(MIDS[mid][0], iso, mid)

    def test_no_duplicate_or_malformed_rows(self):
        self.assertTrue(all(len(m) == 3 and m.isdigit() and len(v[0]) in (2, 5) for m, v in MIDS.items()))
        self.assertGreater(len(MIDS), 250)


class Positions(unittest.TestCase):
    def test_valid(self):
        p = lc.parse_position(pos_msg("265123456", 57.7, 11.9), MIDS)
        self.assertEqual((p["mmsi"], p["lat"], p["lon"], p["sog"], p["cog"]), ("265123456", 57.7, 11.9, 10.0, 90.0))

    def test_class_b(self):
        self.assertIsNotNone(lc.parse_position(pos_msg("636123456", 1.2, 103.8, kind="StandardClassBPositionReport"), MIDS))

    def test_rejects(self):
        for case, msg in {
            "lat 91": pos_msg("265123456", 91, 11.9), "lon 181": pos_msg("265123456", 57, 181),
            "0,0": pos_msg("265123456", 0, 0), "lat 95": pos_msg("265123456", 95, 11),
            "coast station": pos_msg("002655001", 57, 11), "aton": pos_msg("992651234", 57, 11),
            "aircraft": pos_msg("111265001", 57, 11), "unknown mid": pos_msg("999123456", 57, 11),
            "short mmsi": pos_msg("26512345", 57, 11), "nan": pos_msg("265123456", float("nan"), 11),
        }.items():
            self.assertIsNone(lc.parse_position(msg, MIDS), case)

    def test_not_available_values_become_none(self):
        p = lc.parse_position(pos_msg("265123456", 57, 11, sog=102.3, cog=360.0), MIDS)
        self.assertIsNone(p["sog"])
        self.assertIsNone(p["cog"])

    def test_mixed_case_keys(self):
        msg = {"messagetype": "PositionReport", "metadata": {"mmsi": 265123456},
               "message": {"positionreport": {"userid": 265123456, "latitude": 57.0, "longitude": 11.0}}}
        self.assertIsNotNone(lc.parse_position(msg, MIDS))


class Statics(unittest.TestCase):
    def test_class_a(self):
        r = lc.parse_static(static_msg("265123456", 70, 120), MIDS)
        self.assertEqual((r["ship_type"], r["length_m"], r["beam_m"]), (70, 120, 8))

    def test_class_b(self):
        msg = {"MessageType": "StaticDataReport", "MetaData": {"MMSI": 257123456},
               "Message": {"StaticDataReport": {"UserID": 257123456,
                           "ReportA": {"Name": "BAT"}, "ReportB": {"ShipType": 37, "Dimension": {"A": 5, "B": 4}}}}}
        r = lc.parse_static(msg, MIDS)
        self.assertEqual((r["name"], r["ship_type"], r["length_m"]), ("BAT", 37, 9))

    def test_zero_means_missing(self):
        r = lc.parse_static(static_msg("265123456", 0, 0), MIDS)
        self.assertIsNone(r["ship_type"])
        self.assertIsNone(r["length_m"])

    def test_eta(self):
        self.assertEqual(lc.eta_text({"Month": 10, "Day": 2, "Hour": 8, "Minute": 5}), "10-02 08:05")
        self.assertIsNone(lc.eta_text({"Month": 0, "Day": 0}))


class Exclusion(unittest.TestCase):
    def make(self):
        return lc.State(MIDS, lc.Thinner())

    def test_excluded_types_never_kept(self):
        for t in (35, 51, 55, 58):
            st = self.make()
            m = f"265{t:06d}"
            st.handle(pos_msg(m, 57, 11), 1000.0)         # position kommer före typen
            self.assertEqual(len(st.positions), 1)
            st.handle(static_msg(m, t), 1001.0)
            self.assertEqual(st.positions, [])
            self.assertNotIn(m, st.vessels)
            self.assertIn(m, st.excluded)
            st.handle(pos_msg(m, 57, 11), 1500.0)
            self.assertEqual(st.positions, [])
            self.assertEqual(st.new_excluded[m], f"ais_type:{t}")

    def test_ordinary_types_kept(self):
        st = self.make()
        for t in (30, 36, 52, 60, 70, 80, 90):
            st.handle(static_msg(f"265{t:06d}", t), 1000.0)
        self.assertEqual(len(st.vessels), 7)
        self.assertEqual(st.excluded, set())


class Thinning(unittest.TestCase):
    def test_first_always_kept(self):
        self.assertTrue(lc.Thinner().accept("a", 0, 57, 11, 10, 90))

    def test_min_gap_drops(self):
        t = lc.Thinner(min_gap_s=30)
        t.accept("a", 0, 57, 11, 10, 90)
        self.assertFalse(t.accept("a", 10, 57.001, 11, 10, 180))  # även vid kursändring

    def test_moving_max_age(self):
        t = lc.Thinner(min_gap_s=30, moving_max_s=180)
        t.accept("a", 0, 57, 11, 10, 90)
        self.assertFalse(t.accept("a", 100, 57, 11.01, 10, 90))
        self.assertTrue(t.accept("a", 181, 57, 11.02, 10, 90))

    def test_still_vessel_sparse(self):
        t = lc.Thinner(min_gap_s=30, still_max_s=1800)
        t.accept("a", 0, 57, 11, 0.0, 90)
        kept = [t.accept("a", s, 57, 11, 0.0, 90) for s in range(10, 1800, 10)]
        self.assertEqual(sum(kept), 0)
        self.assertTrue(t.accept("a", 1800, 57, 11, 0.0, 90))

    def test_turn_keeps(self):
        t = lc.Thinner(min_gap_s=30, turn_deg=25)
        t.accept("a", 0, 57, 11, 10, 350)
        self.assertTrue(t.accept("a", 40, 57, 11.001, 10, 20))   # 30 graders

    def test_turn_wraparound(self):
        self.assertAlmostEqual(lc.angle_diff(350, 10), 20)

    def test_jump_keeps(self):
        t = lc.Thinner(min_gap_s=30, move_m=1000)
        t.accept("a", 0, 57, 11, 0.0, 90)
        self.assertTrue(t.accept("a", 40, 57.02, 11, 0.0, 90))   # cirka 2,2 km

    def test_vessels_are_independent(self):
        t = lc.Thinner(min_gap_s=30)
        t.accept("a", 0, 57, 11, 10, 90)
        self.assertTrue(t.accept("b", 1, 57, 11, 10, 90))

    def test_prune(self):
        t = lc.Thinner()
        t.accept("a", 0, 57, 11, 10, 90)
        t.prune(10000, older_than_s=7200)
        self.assertEqual(t.last, {})

    def test_haversine(self):
        self.assertAlmostEqual(lc.haversine_m(57, 11, 58, 11) / 1000, 111.2, delta=0.3)


if __name__ == "__main__":
    unittest.main(verbosity=1)
