#!/usr/bin/env python3
"""
Tests for calibrate.py: annotate simulated flights whose parameters are known
and check that the calibration recovers them.
Run with: python -m pytest test_calibrate.py -v
"""

import csv
import os
import tempfile
import unittest

from calibrate import (
    calibrated_config, estimate, quantile_table, read_flights, synthetic_annotations, write_csv,
)
from simulate_deboarding import Config, Simulator, sample_quantiles


def median_of(table):
    return sample_quantiles(table, 0.5)


class TestSyntheticRecovery(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        rows = []
        for i in range(2):
            rows += synthetic_annotations(Simulator(Config(), seed=2000 + i), f"sim-{i}")
        fd, cls.path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        write_csv(rows, cls.path)
        cls.flights = read_flights([cls.path])
        est, cls.notes = estimate(cls.flights)
        cls.est = {e.param: e.value for e in est}

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.path)

    def test_everything_estimable_is_estimated(self):
        for param in ("prep_time", "stand_time", "bag_time", "p_bag", "p_prefetch",
                      "bin_offsets", "p_yield", "door_headway"):
            self.assertIn(param, self.est, self.notes)

    def test_bag_time_recovered(self):
        truth = median_of(Config().bag_time)
        self.assertAlmostEqual(median_of(self.est["bag_time"]), truth, delta=1.0)

    def test_prep_time_recovered(self):
        self.assertAlmostEqual(median_of(self.est["prep_time"]), median_of(Config().prep_time), delta=0.6)

    def test_shares_recovered(self):
        cfg = Config()
        self.assertAlmostEqual(self.est["p_bag"], cfg.p_bag, delta=0.06)
        self.assertAlmostEqual(self.est["p_prefetch"], cfg.p_prefetch, delta=0.12)
        self.assertGreater(self.est["p_yield"], 0.9)          # simulated with courtesy
        self.assertAlmostEqual(self.est["door_headway"], cfg.door_headway, delta=0.25)
        share_own_row = dict(self.est["bin_offsets"])[0]
        self.assertAlmostEqual(share_own_row, dict(cfg.bin_offsets)[0], delta=0.07)

    def test_calibrated_config_runs(self):
        cfg = calibrated_config([])
        self.assertEqual(cfg, Config())
        sim = Simulator(calibrated_config(
            [type("E", (), {"param": k, "value": v})() for k, v in self.est.items()]), seed=1, rows=6)
        self.assertGreater(sim.run(), 0)


class TestFormat(unittest.TestCase):

    def write(self, lines):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", newline="") as f:
            f.write("video,t,who,event,value,note\n" + "\n".join(lines) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_reads_people_bins_and_meta(self):
        path = self.write([
            "v,0,,belt_off,,", "v,0,,meta,rows=30,", "v,4.2,12c,stand,,",
            "v,8.1,12C,bin_open,13,", "v,15.3,12C,bag_down,,", "v,61.4,12C,exit,,", "v,62.5,,door,,",
        ])
        (fl,) = read_flights([path])
        self.assertEqual(fl.meta["rows"], "30")
        p = fl.people["12C"]
        self.assertEqual((p.row, p.col, p.bin_row), (11, "C", 12))
        self.assertEqual(fl.exits, [61.4, 62.5])

    def test_unknown_event_is_rejected(self):
        with self.assertRaises(SystemExit):
            read_flights([self.write(["v,1,12C,jump,,"])])

    def test_quantile_table(self):
        self.assertEqual(quantile_table([1, 2, 3, 4, 5])[3], (0.5, 3))
        self.assertEqual(quantile_table([10])[0], (0.0, 10))


if __name__ == "__main__":
    unittest.main(verbosity=2)
