#!/usr/bin/env python3
"""
Unit tests for the deboarding simulation.
Run with: python -m pytest test_simulation.py -v
Or:       python test_simulation.py

These tests check invariants and hand-computed cases only.  They do NOT
assert which policy wins -- that is what the simulation is for.
"""

import unittest
from dataclasses import replace

from simulate_deboarding import (
    Config, PaxState, Rng, Simulator, compute_stats, paired_ci, run_experiment,
    sample_discrete, sample_quantiles,
)

# 0.5 s ticks, instant acceleration: motion is easy to trace by hand
HAND = dict(dt=0.5, accel=100.0)


def pax(row, col, **kw):
    spec = dict(row=row, col=col, stand=1.0, lateral=1.0, step_in=1.0, v0=1.0, depth=0.34)
    spec.update(kw)
    return spec


class TestHandComputed(unittest.TestCase):

    def test_single_passenger(self):
        """1C, no bag, 2 m from the door at 1 m/s.
        t0.5 TO_EDGE (stand 1 s) -> t1.5 AT_EDGE, steps in at x=2.0 ->
        t2.5 walking: 1.5, 1.0, 0.5, 0.0 at t4.0 -> exits at t4.5."""
        sim = Simulator(rows=1, manifest=[pax(0, "C")], **HAND)
        self.assertEqual(sim.run(), 4.5)

    def test_middle_seat_waits_for_aisle_seat(self):
        sim = Simulator(rows=1, manifest=[pax(0, "B"), pax(0, "C")], **HAND)
        sim.run()
        b, c = sim.passengers
        self.assertLess(c.t_enter, b.t_enter)
        # B only starts moving once C is in the aisle: stand + one seat = 2 s
        self.assertGreaterEqual(b.t_enter - c.t_enter, int(2.0 / 0.5))

    def test_bag_in_aisle_costs_its_time(self):
        no_bag = Simulator(rows=1, manifest=[pax(0, "C")], **HAND).run()
        bag = Simulator(rows=1, manifest=[pax(0, "C", has_bag=True, bag=3.0)], **HAND).run()
        # 3 s retrieving, then 0.25 m further forward and 15 % slower walking
        self.assertGreaterEqual(bag - no_bag, 3.0)
        self.assertLess(bag - no_bag, 4.5)

    def test_prefetch_moves_bag_time_out_of_the_aisle(self):
        """Taking the bag down while waiting at the edge only helps if you
        have to wait anyway: here 1B waits while 1C takes their bag in the
        aisle right in front of the row."""
        def run(prefetch):
            sim = Simulator(rows=1, manifest=[
                pax(0, "C", has_bag=True, bag=6.0),
                pax(0, "B", has_bag=True, bag=6.0, prefetch=prefetch),
            ], **HAND)
            sim.run()
            return sim.passengers[1].t_exit * sim.dt
        self.assertLess(run(True), run(False) - 3.0)

    def test_courtesy_lets_the_row_ahead_out_first(self):
        """2C is already walking when 1C gets up: courtesy lets 1C go first."""
        def order(p_yield):
            sim = Simulator(rows=2, p_yield=p_yield, manifest=[
                pax(0, "C", prep=2.5), pax(1, "C", prep=0.0),
            ], **HAND)
            sim.run()
            return [p.row for p in sorted(sim.passengers, key=lambda p: p.t_exit)]
        self.assertEqual(order(1.0), [0, 1])
        self.assertEqual(order(0.0), [1, 0])

    def test_bag_behind_walks_back_against_the_flow(self):
        """1C's bag is above row 4.  3C is already walking out when 1C gets
        up, so 1C waits until the aisle behind is clear, then walks back."""
        sim = Simulator(rows=4, p_yield=0.0, manifest=[
            pax(0, "C", has_bag=True, bag=2.0, bin_offset=3, prep=3.0),
            pax(2, "C"),
        ], **HAND)
        back = sim.passengers[0]
        seen_back = False
        while sim.n_inside:
            sim.step()
            if back.state == PaxState.BACKTRACK:
                seen_back = True
                self.assertEqual(len(sim.reservations), 1)
        self.assertTrue(seen_back)
        self.assertLess(sim.passengers[1].t_exit, back.t_exit)

    def test_empty_plane(self):
        sim = Simulator(p_occupancy=0.0, seed=1, rows=5)
        self.assertEqual(len(sim.passengers), 0)
        self.assertEqual(sim.run(), 0)


class TestInvariants(unittest.TestCase):
    """Run random planes tick by tick and check physical invariants."""

    def check_run(self, **kw):
        sim = Simulator(rows=10, **kw)
        cfg = sim.cfg
        ever_ahead: set[tuple[int, int]] = set()
        while sim.n_inside:
            sim.step()
            aisle = sim.aisle
            xs = [p.x for p in aisle]
            self.assertEqual(xs, sorted(xs), "aisle not sorted")
            for a, b in zip(aisle, aisle[1:]):
                gap = (b.x - b.length / 2) - (a.x + a.length / 2)
                self.assertGreaterEqual(gap, cfg.s0 - 1e-9,
                                        f"overlap at t={sim.time}: {a.pid}/{b.pid} gap {gap:.3f}")
            for p in aisle:
                self.assertIn(p.state, (PaxState.ENTERING, PaxState.BACKTRACK,
                                        PaxState.RETRIEVING, PaxState.WALKING))
                self.assertGreaterEqual(p.x, 0.0)
            # nobody overtakes
            pids = [p.pid for p in aisle]
            for i, a in enumerate(pids):
                for b in pids[i + 1:]:
                    self.assertNotIn((b, a), ever_ahead, f"{b} and {a} swapped at t={sim.time}")
                    ever_ahead.add((a, b))
            # reserved stretches stay empty except for their owner
            for res in sim.reservations:
                for p in aisle:
                    if p is not res.owner:
                        self.assertFalse(p.x - p.length / 2 < res.b and p.x + p.length / 2 > res.a,
                                         f"{p.pid} inside a reservation at t={sim.time}")
            self.assertLess(sim.tick, 40000, "simulation stuck")

        self.assertTrue(all(p.state == PaxState.EXITED for p in sim.passengers))
        for p in sim.passengers:
            for b in p.blockers:
                self.assertLess(b.t_enter, p.t_enter)
        exits = sorted(p.t_exit for p in sim.passengers)
        for a, b in zip(exits, exits[1:]):
            self.assertGreaterEqual(b - a, sim.door_ticks)

    def test_invariants_random_planes(self):
        for seed in range(4):
            for p_yield in (0.0, 0.5, 1.0):
                with self.subTest(seed=seed, p_yield=p_yield):
                    self.check_run(seed=seed, p_yield=p_yield, p_occupancy=0.85)

    def test_regression_bag_put_down_into_reserved_stretch(self):
        """Seed that used to deadlock: someone finishing their bag right
        behind a reserved stretch stepped into it, face to face with the
        passenger walking back to their bin."""
        sim = Simulator(p_yield=1.0, seed=1955374602)
        sim.run(max_ticks=20000)
        self.assertEqual(sim.n_inside, 0)

    def test_invariants_many_bags_elsewhere(self):
        spread = ((-3, 0.15), (-1, 0.15), (0, 0.2), (1, 0.15), (2, 0.15), (3, 0.2))
        for seed, p_yield in ((7, 1.0), (8, 0.0)):
            with self.subTest(seed=seed):
                self.check_run(seed=seed, p_yield=p_yield, p_bag=0.9, bin_offsets=spread)


class TestModelProperties(unittest.TestCase):

    def test_same_passengers_for_any_policy(self):
        """Common random numbers: p_yield must not change who is on board."""
        sig = lambda s: [(p.row, p.col, p.has_bag, p.bin_row, p.prefetch, p.prep, p.to_edge,
                          p.step_in, p.bag, p.v0, p.depth) for p in s.passengers]
        self.assertEqual(sig(Simulator(rows=10, p_yield=0.0, seed=7)),
                         sig(Simulator(rows=10, p_yield=1.0, seed=7)))

    def test_door_bottleneck_hides_policy(self):
        """If the door is much slower than the aisle, both policies converge."""
        cfg = replace(Config(), rows=8, door_headway=12.0)
        court = [r["total"] for r in run_experiment(1.0, k=4, seed=3, config=cfg)]
        aisle = [r["total"] for r in run_experiment(0.0, k=4, seed=3, config=cfg)]
        self.assertLess(abs(sum(court) - sum(aisle)) / sum(aisle), 0.03)

    def test_no_bags_no_prep_is_door_limited(self):
        for p_yield in (0.0, 1.0):
            sim = Simulator(rows=6, p_bag=0.0, prep_time=((0, 0.0), (1, 0.0)),
                            door_headway=6.0, p_yield=p_yield, seed=1)
            n = len(sim.passengers)
            self.assertAlmostEqual(sim.run(), 6.0 * (n - 1), delta=10.0)


class TestSampling(unittest.TestCase):

    def test_mulberry32_reference_values(self):
        """Pinned values; the JS port in web/index.html must match them."""
        r = Rng(42)
        self.assertEqual([r.next_u32() for _ in range(3)],
                         [2581720956, 1925393290, 3661312704])

    def test_quantiles(self):
        q = ((0.0, 1.0), (0.5, 2.0), (1.0, 4.0))
        self.assertEqual(sample_quantiles(q, 0.0), 1.0)
        self.assertEqual(sample_quantiles(q, 0.25), 1.5)
        self.assertEqual(sample_quantiles(q, 0.75), 3.0)
        self.assertLessEqual(sample_quantiles(q, 0.999999), 4.0)

    def test_discrete(self):
        table = ((-1, 0.2), (0, 0.5), (2, 0.3))
        self.assertEqual(sample_discrete(table, 0.1), -1)
        self.assertEqual(sample_discrete(table, 0.6), 0)
        self.assertEqual(sample_discrete(table, 0.95), 2)


class TestStatistics(unittest.TestCase):

    def test_compute_stats(self):
        stats = compute_stats([10, 20, 30, 40, 50])
        self.assertEqual(stats["n"], 5)
        self.assertAlmostEqual(stats["mean"], 30.0)
        self.assertEqual(stats["min"], 10)
        self.assertEqual(stats["max"], 50)

    def test_paired_ci_constant_difference(self):
        a = [100, 150, 90, 300]
        self.assertEqual(paired_ci(a, [x - 5 for x in a], seed=1), (5, 5, 5))

    def test_paired_ci_contains_zero_for_noise(self):
        r = Rng(3)
        a = [r.random() for _ in range(300)]
        b = [r.random() for _ in range(300)]
        _, lo, hi = paired_ci(a, b, seed=3)
        self.assertLess(lo, 0)
        self.assertGreater(hi, 0)


class TestExperimentRunner(unittest.TestCase):

    def test_reproducibility(self):
        cfg = replace(Config(), rows=5)
        self.assertEqual(run_experiment(0.5, k=4, seed=42, config=cfg),
                         run_experiment(0.5, k=4, seed=42, config=cfg))

    def test_different_seeds_differ(self):
        cfg = replace(Config(), rows=6)
        self.assertNotEqual(run_experiment(0.0, k=4, seed=1, config=cfg),
                            run_experiment(0.0, k=4, seed=2, config=cfg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
