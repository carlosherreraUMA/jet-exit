#!/usr/bin/env python3
"""
Unit tests for the deboarding simulation.
Run with: python -m pytest test_simulation.py -v
Or:       python test_simulation.py

These tests check invariants and hand-computed cases only.  They do NOT
assert which policy wins -- that is what the simulation is for.
"""

import unittest

from simulate_deboarding import (
    BLOCKING_COLUMNS, PaxState, Rng, Simulator, compute_stats,
    paired_ci, run_experiment,
)

# 1 s ticks, one cell per row, one cell before the door: easy to trace by hand
HAND = dict(dt=1.0, cells_per_row=1, front_cells=1)


def two_rows(p_yield, bag=0, prefetch=False):
    """Row 1 C (prep 2 s, optional bag) and row 2 C (ready at once, no bag).

    Row 2 C enters at t=1, starts walking at t=2 and reaches row 1 just as
    row 1 C becomes ready: the only thing that differs is whether they yield.
    """
    return Simulator(rows=2, p_yield=p_yield, **HAND, manifest=[
        dict(row=0, col="C", has_bag=bag > 0, prep=2, bag=bag, prefetch=prefetch),
        dict(row=1, col="C", prep=0),
    ])


class TestHandComputed(unittest.TestCase):
    """Small cases traced tick by tick (see comments for the timeline)."""

    def test_single_passenger(self):
        # t1 WAITING+enter cell 1, t2 walking -> cell 0, t3 exits
        sim = Simulator(rows=1, **HAND, manifest=[dict(row=0, col="C")])
        self.assertEqual(sim.run(), 3)

    def test_yield_without_bag(self):
        # No yield: 2C passes (exits t4), 1C enters t3 behind, exits t5.
        self.assertEqual(two_rows(p_yield=0.0).run(), 5)
        # Yield at t2: 1C enters in front, exits t4; 2C restarts, exits t6.
        self.assertEqual(two_rows(p_yield=1.0).run(), 6)

    def test_yield_with_bag(self):
        # The 3 s bag is taken in the aisle; with courtesy 2C waits behind it.
        self.assertEqual(two_rows(p_yield=0.0, bag=3).run(), 8)
        self.assertEqual(two_rows(p_yield=1.0, bag=3).run(), 9)

    def test_prefetch_saves_aisle_time(self):
        # 1C takes 1 s of the bag while waiting at the seat (t3), rest in aisle.
        self.assertEqual(two_rows(p_yield=0.0, bag=3, prefetch=True).run(), 7)

    def test_yield_order_of_exit(self):
        sim = two_rows(p_yield=1.0)
        sim.run()
        first, second = sorted(sim.passengers, key=lambda p: p.t_exit)
        self.assertEqual((first.row, second.row), (0, 1))

    def test_empty_plane(self):
        sim = Simulator(rows=5, p_occupancy=0.0, seed=1)
        self.assertEqual(len(sim.passengers), 0)
        self.assertEqual(sim.run(), 0)


class TestInvariants(unittest.TestCase):
    """Run random planes tick by tick and check physical invariants."""

    def check_run(self, **kw):
        sim = Simulator(rows=8, **kw)
        ahead_of: set[tuple[int, int]] = set()
        while sim.n_inside:
            sim.step()
            in_aisle = [p for p in sim.aisle if p is not None]
            # one cell per person, only people who left their seat
            self.assertEqual(len(in_aisle), len({p.pid for p in in_aisle}))
            for p in in_aisle:
                self.assertIn(p.state, (PaxState.ENTERING, PaxState.RETRIEVING, PaxState.WALKING))
            # nobody overtakes: relative order in the aisle never flips
            pids = [p.pid for p in in_aisle]
            for i, a in enumerate(pids):
                for b in pids[i + 1:]:
                    self.assertNotIn((b, a), ahead_of, f"{b} and {a} swapped at t={sim.time}")
                    ahead_of.add((a, b))
            self.assertLess(sim.tick, 20000)

        # everyone is out, once
        self.assertTrue(all(p.state == PaxState.EXITED for p in sim.passengers))
        # inner seats leave the row before outer ones
        for p in sim.passengers:
            for bcol in BLOCKING_COLUMNS[p.col]:
                blocker = sim._pax_by_row_col.get((p.row, bcol))
                if blocker is not None:
                    self.assertLess(blocker.t_enter, p.t_enter)
        # door never faster than its headway
        exits = sorted(p.t_exit for p in sim.passengers)
        for a, b in zip(exits, exits[1:]):
            self.assertGreaterEqual(b - a, sim.door_ticks)
        # the metrics agree with the run
        if exits:
            self.assertEqual(sim.metrics()["total"], exits[-1] * sim.dt)

    def test_invariants_random_planes(self):
        for seed in range(6):
            for p_yield in (0.0, 0.5, 1.0):
                with self.subTest(seed=seed, p_yield=p_yield):
                    self.check_run(seed=seed, p_yield=p_yield, p_occupancy=0.8)

    def test_invariants_other_geometries(self):
        for cpr in (1.0, 2.0):
            with self.subTest(cells_per_row=cpr):
                self.check_run(seed=3, p_yield=1.0, cells_per_row=cpr, door_headway=2.0)


class TestModelProperties(unittest.TestCase):

    def test_same_passengers_for_any_policy(self):
        """Common random numbers: p_yield must not change who is on board."""
        a = Simulator(rows=10, p_yield=0.0, seed=7)
        b = Simulator(rows=10, p_yield=1.0, seed=7)
        sig = lambda s: [(p.row, p.col, p.has_bag, p.prep, p.bag, p.prefetch) for p in s.passengers]
        self.assertEqual(sig(a), sig(b))

    def test_door_bottleneck_hides_policy(self):
        """If the door is much slower than the aisle, both policies converge."""
        kw = dict(rows=10, door_headway=8.0, k=10, seed=3)
        court = [r["total"] for r in run_experiment(1.0, **kw)]
        aisle = [r["total"] for r in run_experiment(0.0, **kw)]
        rel = abs(sum(court) - sum(aisle)) / sum(aisle)
        self.assertLess(rel, 0.03)

    def test_no_bags_no_prep_is_door_limited(self):
        """Without bags or preparation and a slow door, 60 people leave at
        exactly the door rate under any policy."""
        for p_yield in (0.0, 1.0):
            sim = Simulator(rows=10, p_bag=0.0, prep_range=(0, 0, 0), door_headway=4.0,
                            p_yield=p_yield, seed=1)
            total = sim.run()
            self.assertGreaterEqual(total, 4.0 * (len(sim.passengers) - 1))
            self.assertLess(total, 4.0 * (len(sim.passengers) - 1) + 5)


class TestRng(unittest.TestCase):

    def test_mulberry32_reference_values(self):
        """Pinned values; the JS port in web/index.html must match them."""
        r = Rng(42)
        self.assertEqual([r.next_u32() for _ in range(3)],
                         [2581720956, 1925393290, 3661312704])

    def test_triangular_bounds(self):
        r = Rng(1)
        xs = [r.triangular(2, 15, 5) for _ in range(2000)]
        self.assertTrue(all(2 <= x <= 15 for x in xs))
        self.assertAlmostEqual(sum(xs) / len(xs), (2 + 15 + 5) / 3, delta=0.3)


class TestStatistics(unittest.TestCase):

    def test_compute_stats(self):
        stats = compute_stats([10, 20, 30, 40, 50])
        self.assertEqual(stats["n"], 5)
        self.assertAlmostEqual(stats["mean"], 30.0)
        self.assertAlmostEqual(stats["median"], 30.0)
        self.assertEqual(stats["min"], 10)
        self.assertEqual(stats["max"], 50)

    def test_paired_ci_constant_difference(self):
        a = [100, 150, 90, 300]
        diff, lo, hi = paired_ci(a, [x - 5 for x in a], seed=1)
        self.assertEqual((diff, lo, hi), (5, 5, 5))

    def test_paired_ci_contains_zero_for_noise(self):
        r = Rng(3)
        a = [r.random() for _ in range(300)]
        b = [r.random() for _ in range(300)]
        _, lo, hi = paired_ci(a, b, seed=3)
        self.assertLess(lo, 0)
        self.assertGreater(hi, 0)


class TestExperimentRunner(unittest.TestCase):

    def test_reproducibility(self):
        r1 = run_experiment(0.5, k=10, seed=42, rows=5)
        r2 = run_experiment(0.5, k=10, seed=42, rows=5)
        self.assertEqual(r1, r2)

    def test_different_seeds_differ(self):
        r1 = run_experiment(0.0, k=10, seed=1, rows=10)
        r2 = run_experiment(0.0, k=10, seed=2, rows=10)
        self.assertNotEqual(r1, r2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
