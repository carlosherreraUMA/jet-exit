#!/usr/bin/env python3
"""
=============================================================================
  Airplane Single-Aisle Deboarding Simulator
  Agent-based cellular model + Monte Carlo
=============================================================================

Question: is deboarding faster if people already standing in the aisle (bags
in hand) keep walking, and seated people wait for a real gap -- instead of
the usual courtesy where the aisle stops to let each row out?

The policy is a single behavioural parameter:

  p_yield = probability that a walking passenger stops to let a waiting
            seated passenger into the aisle in front of them.
            1.0 -> "courtesy"        (today's etiquette)
            0.0 -> "aisle_priority"  (the hypothesis)

Usage examples:
  python simulate_deboarding.py                      # courtesy vs aisle_priority
  python simulate_deboarding.py --k 500 --seed 1
  python simulate_deboarding.py sweep --k 200 --seed 1 --csv sweep.csv
  python simulate_deboarding.py --rows 3 --k 1 --debug --seed 42

=============================================================================
Model (every choice here is a parameter; defaults target an A320, 30 rows 3-3)

1. Time and space.  1 tick = `dt` = 0.5 s.  The aisle is a line of cells,
   `cells_per_row` = 2 per seat row (~0.38 m each for a 0.76 m pitch, one
   standing adult in a queue), so two people from the same row can stand
   in the aisle at once.  `front_cells` extra cells separate row 1 from the
   door; cell 0 is next to the door.  A walker moves at most 1 cell/tick
   (~0.76 m/s).  All inputs and outputs are in seconds.

2. Passenger life cycle:
     SEATED     -- doing what can be done sitting down (belt, phone, jacket,
                   under-seat bag): `prep` seconds, everyone in parallel
                   from t=0.
     WAITING    -- prep done and nobody left between them and the aisle
                   (C/D first, then B/E, then A/F).  Wants to enter.  A
                   fraction `p_prefetch` of bag owners can already take
                   their bag here, standing at their seat while they wait.
     ENTERING   -- occupies their row's aisle cell for `enter` seconds
                   (1 s aisle seat, 2 s middle, 3 s window).
     RETRIEVING -- whatever bag time is left: standing in their row's
                   aisle cell, blocking it.
     WALKING    -- moves toward the door when the cell ahead is free.
     EXITED
   Bag time spent while the queue ahead is stopped anyway costs nothing --
   which is exactly the effect the hypothesis relies on.

   Calibration: with p_prefetch = 0 (bags only from the aisle) courtesy
   takes ~17 min for a full A320, far above the 15-20 pax/min per door
   observed in practice (9-12 min).  p_prefetch = 0.8 brings courtesy to
   ~11 min.  `sweep` shows how the conclusion depends on it.

3. Movement (front to back each tick).  A walker advances if the cell ahead
   is free.  A walker that was already moving may follow the person ahead
   in the same tick (a moving platoon stays compact); a stopped walker
   only starts once the cell ahead was free at the start of the tick
   (one tick of reaction, so a jam restarts as a wave).

4. Yielding.  When a walker is about to step into a row's aisle cell and
   someone from that row is WAITING, the walker decides once (per cell)
   whether to yield, with probability `p_yield`.  If they yield, they stay
   put and the seated passenger enters that cell in front of them.

5. Gap acceptance (any policy).  A WAITING passenger may also enter their
   row's cells without anyone yielding if it is free and the nearest
   person behind within `gap_cells` cells is not a walker (nobody coming,
   or whoever is behind is stuck entering/retrieving, or has just stopped
   to let this row out).

6. Door.  The walker in cell 0 leaves if at least `door_headway` seconds
   have passed since the previous exit.

7. Randomness.  A small portable PRNG (mulberry32) is used so the web
   version (web/index.html) reproduces runs exactly.  Passenger generation
   and yield decisions use separate streams, so different p_yield values
   see the same passengers (common random numbers -> paired comparisons).
=============================================================================
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
from concurrent.futures import Executor, ProcessPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# ---------------------------------------------------------------------------
# Portable RNG (bit-identical to the JS port)
# ---------------------------------------------------------------------------

_M32 = 0xFFFFFFFF


class Rng:
    """mulberry32 -- tiny, fast and trivially reproducible in JavaScript."""

    def __init__(self, seed: int):
        self.a = seed & _M32

    def next_u32(self) -> int:
        self.a = (self.a + 0x6D2B79F5) & _M32
        a = self.a
        t = ((a ^ (a >> 15)) * (1 | a)) & _M32
        t = ((t + (((t ^ (t >> 7)) * (61 | t)) & _M32)) & _M32) ^ t
        return (t ^ (t >> 14)) & _M32

    def random(self) -> float:
        return self.next_u32() / 4294967296

    def triangular(self, low: float, high: float, mode: float) -> float:
        u = self.random()
        if high == low:
            return low
        c = (mode - low) / (high - low)
        if u > c:
            u = 1.0 - u
            c = 1.0 - c
            low, high = high, low
        return low + (high - low) * math.sqrt(u * c)


def _to_ticks(seconds: float) -> int:
    # floor(x + 0.5) rather than round(): identical to JS Math.floor(x + 0.5)
    return int(math.floor(seconds + 0.5))


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class PaxState(Enum):
    SEATED = auto()
    WAITING = auto()
    ENTERING = auto()
    RETRIEVING = auto()
    WALKING = auto()
    EXITED = auto()


# Columns: A B C | aisle | D E F
COLUMN_AISLE_DISTANCE = {"C": 0, "D": 0, "B": 1, "E": 1, "A": 2, "F": 2}
LEFT_HALF = {"A", "B", "C"}
RIGHT_HALF = {"D", "E", "F"}
BLOCKING_COLUMNS = {
    "A": ["B", "C"],
    "B": ["C"],
    "C": [],
    "D": [],
    "E": ["D"],
    "F": ["D", "E"],
}
OUT_OF_SEAT = (PaxState.RETRIEVING, PaxState.WALKING, PaxState.EXITED)

POLICIES = {"courtesy": 1.0, "aisle_priority": 0.0}


@dataclass
class Passenger:
    pid: int
    row: int            # 0-indexed (row 0 = front)
    col: str            # A..F
    has_bag: bool
    prep: int           # ticks of seated preparation
    enter: int          # ticks to step from seat into the aisle
    bag: int            # ticks to take the overhead bag (0 if none)
    prefetch: bool = False          # can take the bag while WAITING at the seat
    state: PaxState = PaxState.SEATED
    prep_left: int = 0
    enter_left: int = 0
    bag_left: int = 0
    moving: bool = False            # advanced a cell on the previous tick
    yields: dict[int, bool] = field(default_factory=dict)  # cell -> decision
    t_ready: int = -1               # tick they became WAITING
    t_enter: int = -1               # tick they stepped into the aisle
    t_exit: int = -1

    def __post_init__(self):
        self.prep_left = self.prep
        self.bag_left = self.bag

    @property
    def is_left(self) -> bool:
        return self.col in LEFT_HALF


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class Simulator:
    """Tick-based simulator for single-aisle deboarding."""

    def __init__(
        self,
        rows: int = 30,
        p_occupancy: float = 1.0,
        p_bag: float = 0.65,
        p_prefetch: float = 0.8,
        prep_range: tuple[float, float, float] = (0, 10, 2),   # min, max, mode
        bag_range: tuple[float, float, float] = (2, 15, 5),    # min, max, mode
        enter_base: float = 1.0,
        enter_per_seat: float = 1.0,
        dt: float = 0.5,
        cells_per_row: float = 1.5,
        front_cells: int = 2,
        door_headway: float = 1.0,
        gap_cells: int = 2,
        p_yield: float = 1.0,
        seed: int = 0,
        manifest: list[dict] | None = None,
        debug: bool = False,
    ):
        self.rows = rows
        self.p_occupancy = p_occupancy
        self.p_bag = p_bag
        self.p_prefetch = p_prefetch
        self.prep_range = prep_range
        self.bag_range = bag_range
        self.enter_base = enter_base
        self.enter_per_seat = enter_per_seat
        self.dt = dt
        self.cells_per_row = cells_per_row
        self.front_cells = front_cells
        self.door_headway = door_headway
        self.door_ticks = max(1, self._ticks(door_headway))
        self.gap_cells = gap_cells
        self.p_yield = p_yield
        self.debug = debug

        self.gen_rng = Rng(seed)
        self.dyn_rng = Rng(seed ^ 0x9E3779B9)

        self.tick = 0
        self.last_exit = -math.inf
        self._yielders: set[int] = set()   # walkers that stopped for a row this tick
        self.n_cells = front_cells + math.floor(rows * cells_per_row)
        self.aisle: list[Optional[Passenger]] = [None] * self.n_cells
        # Cells a passenger of row r can step into, front one first.  With a
        # fractional cells_per_row, rows alternate between 1 and 2 cells.
        self.row_cells = [
            list(range(front_cells + math.floor(r * cells_per_row),
                       front_cells + math.floor((r + 1) * cells_per_row)))
            for r in range(rows)
        ]
        self.cell_row: list[Optional[int]] = [None] * self.n_cells
        for r, cells in enumerate(self.row_cells):
            for c in cells:
                self.cell_row[c] = r

        self.passengers: list[Passenger] = []
        self._pax_by_row_col: dict[tuple[int, str], Passenger] = {}
        self._seats_by_row: list[list[Passenger]] = [[] for _ in range(rows)]
        if manifest is None:
            self._generate_passengers()
        else:
            for spec in manifest:
                self.add_passenger(**spec)
        self.n_inside = len(self.passengers)

    # -- Setup ---------------------------------------------------------------

    def _generate_passengers(self) -> None:
        rng = self.gen_rng
        for row in range(self.rows):
            for col in "ABCDEF":
                if rng.random() >= self.p_occupancy:
                    continue
                has_bag = rng.random() < self.p_bag
                prep = rng.triangular(*self.prep_range)
                bag = 0.0
                prefetch = False
                if has_bag:
                    bag = rng.triangular(*self.bag_range)
                    prefetch = rng.random() < self.p_prefetch
                self.add_passenger(row=row, col=col, has_bag=has_bag, prep=prep,
                                   bag=bag, prefetch=prefetch)

    def _ticks(self, seconds: float) -> int:
        return _to_ticks(seconds / self.dt)

    def add_passenger(
        self, row: int, col: str, has_bag: bool = False,
        prep: float = 0, bag: float = 0, enter: float | None = None,
        prefetch: bool = False,
    ) -> Passenger:
        """Add a passenger; times in seconds (converted to whole ticks)."""
        if enter is None:
            enter = self.enter_base + self.enter_per_seat * COLUMN_AISLE_DISTANCE[col]
        pax = Passenger(
            pid=len(self.passengers), row=row, col=col, has_bag=has_bag,
            prep=self._ticks(prep),
            enter=max(1, self._ticks(enter)),
            bag=max(1, self._ticks(bag)) if has_bag else 0,
            prefetch=prefetch and has_bag,
        )
        self.passengers.append(pax)
        self._pax_by_row_col[(row, col)] = pax
        self._seats_by_row[row].append(pax)
        self._seats_by_row[row].sort(key=lambda p: "ABCDEF".index(p.col))
        self.n_inside = len(self.passengers)
        return pax

    # -- Query helpers -------------------------------------------------------

    def _blockers_cleared(self, pax: Passenger) -> bool:
        for bcol in BLOCKING_COLUMNS[pax.col]:
            blocker = self._pax_by_row_col.get((pax.row, bcol))
            if blocker is not None and blocker.state not in OUT_OF_SEAT:
                return False
        return True

    def _waiting_in_row(self, row: int) -> list[Passenger]:
        return [p for p in self._seats_by_row[row] if p.state == PaxState.WAITING]

    def _gap_ok(self, cell: int) -> bool:
        """Nobody walking toward `cell` within gap_cells behind it."""
        for j in range(cell + 1, min(cell + self.gap_cells, self.n_cells - 1) + 1):
            q = self.aisle[j]
            if q is not None:
                return q.state != PaxState.WALKING or q.pid in self._yielders
        return True

    # -- Tick phases ---------------------------------------------------------

    def _phase_timers(self) -> None:
        for p in self.passengers:
            st = p.state
            if st == PaxState.SEATED:
                if p.prep_left > 0:
                    p.prep_left -= 1
                if p.prep_left == 0 and self._blockers_cleared(p):
                    p.state = PaxState.WAITING
                    p.t_ready = self.tick
            elif st == PaxState.WAITING:
                if p.prefetch and p.bag_left > 0:
                    p.bag_left -= 1
            elif st == PaxState.ENTERING:
                p.enter_left -= 1
                if p.enter_left <= 0:
                    p.state = PaxState.RETRIEVING if p.bag_left > 0 else PaxState.WALKING
            elif st == PaxState.RETRIEVING:
                p.bag_left -= 1
                if p.bag_left <= 0:
                    p.state = PaxState.WALKING

    def _phase_door(self) -> None:
        p = self.aisle[0]
        if (p is not None and p.state == PaxState.WALKING
                and self.tick - self.last_exit >= self.door_ticks):
            p.state = PaxState.EXITED
            p.t_exit = self.tick
            p.moving = False
            self.aisle[0] = None
            self.last_exit = self.tick
            self.n_inside -= 1
            if self.debug:
                print(f"  [EXIT] {p.row+1}{p.col} (pid {p.pid})")

    def _phase_move(self) -> set[int]:
        """Advance walkers front to back.  Returns cells offered by yielders."""
        start_occupied = [c is not None for c in self.aisle]
        offered: set[int] = set()
        self._yielders.clear()
        for i in range(1, self.n_cells):
            p = self.aisle[i]
            if p is None:
                continue
            if p.state != PaxState.WALKING or self.aisle[i - 1] is not None:
                p.moving = False
                continue
            if start_occupied[i - 1] and not p.moving:
                continue  # reaction time: was stopped, cell only just freed
            r = self.cell_row[i - 1]
            if r is not None and self._waiting_in_row(r):
                decision = p.yields.get(i - 1)
                if decision is None:
                    decision = self.dyn_rng.random() < self.p_yield
                    p.yields[i - 1] = decision
                if decision:
                    offered.add(i - 1)
                    self._yielders.add(p.pid)
                    p.moving = False
                    if self.debug:
                        print(f"  [YIELD] {p.row+1}{p.col} lets row {r+1} in")
                    continue
            self.aisle[i - 1] = p
            self.aisle[i] = None
            p.moving = True
        return offered

    def _phase_enter(self, offered: set[int]) -> None:
        for r in range(self.rows):
            for c in self.row_cells[r]:
                if self.aisle[c] is not None:
                    continue
                cands = self._waiting_in_row(r)
                if not cands:
                    break
                if c not in offered and not self._gap_ok(c):
                    continue
                # Whoever stood up first goes first; ties alternate sides.
                left_first = (r + self.tick) % 2 == 0
                chosen = min(cands, key=lambda p: (p.t_ready, p.is_left != left_first))
                chosen.state = PaxState.ENTERING
                chosen.enter_left = chosen.enter
                chosen.t_enter = self.tick
                chosen.moving = False
                self.aisle[c] = chosen
                if self.debug:
                    how = "yielded" if c in offered else "gap"
                    print(f"  [ENTER] {chosen.row+1}{chosen.col} -> cell {c} ({how})")

    # -- Main loop -----------------------------------------------------------

    def step(self) -> bool:
        """Execute one tick.  Returns True while someone is still inside."""
        self.tick += 1
        if self.debug:
            print(f"\n--- t={self.tick * self.dt:g}s ---")
        self._phase_timers()
        self._phase_door()
        offered = self._phase_move()
        self._phase_enter(offered)
        if self.debug:
            self._print_state()
        return self.n_inside > 0

    @property
    def time(self) -> float:
        return self.tick * self.dt

    def run(self, max_ticks: int = 50000) -> float:
        """Run to completion.  Returns total seconds (time of the last exit)."""
        while self.n_inside > 0 and self.tick < max_ticks:
            self.step()
        if self.n_inside > 0:
            raise RuntimeError(f"simulation did not finish in {max_ticks} ticks")
        return self.time

    def metrics(self) -> dict:
        """Seconds: last exit, mean exit time and 90th percentile exit time."""
        exits = sorted(p.t_exit * self.dt for p in self.passengers)
        if not exits:
            return {"total": 0.0, "mean_exit": 0.0, "p90_exit": 0.0}
        return {
            "total": self.time,
            "mean_exit": statistics.mean(exits),
            "p90_exit": exits[min(len(exits) - 1, int(len(exits) * 0.9))],
        }

    # -- Debug ---------------------------------------------------------------

    def _print_state(self) -> None:
        glyph = {PaxState.ENTERING: "E", PaxState.RETRIEVING: "B", PaxState.WALKING: ">"}
        cells = "".join(
            "." if p is None else glyph.get(p.state, "?") for p in self.aisle
        )
        print(f"  door|{cells}|  inside={self.n_inside}")


# ---------------------------------------------------------------------------
# Experiment runner
# ---------------------------------------------------------------------------

def run_seeds(k: int, seed: int | None) -> list[int]:
    base = Rng(0 if seed is None else seed)
    return [base.next_u32() for _ in range(k)]


_executor: Executor | None = None   # set by main() to run simulations in parallel


def _run_one(job: tuple[float, int, bool, dict]) -> dict:
    p_yield, seed, debug, sim_kwargs = job
    sim = Simulator(p_yield=p_yield, seed=seed, debug=debug, **sim_kwargs)
    sim.run()
    return sim.metrics()


def run_experiment(
    p_yield: float,
    k: int = 1000,
    seed: int | None = None,
    debug: bool = False,
    **sim_kwargs,
) -> list[dict]:
    """Run K simulations.  The same seed gives the same K planes for any
    p_yield, so results from two calls can be compared run by run."""
    jobs = [(p_yield, s, debug and i == 0, sim_kwargs) for i, s in enumerate(run_seeds(k, seed))]
    if _executor is None or debug:
        return [_run_one(j) for j in jobs]
    return list(_executor.map(_run_one, jobs, chunksize=max(1, k // 64)))


def compute_stats(times: list[float]) -> dict:
    """Summary statistics."""
    times_sorted = sorted(times)
    n = len(times_sorted)
    return {
        "n": n,
        "mean": statistics.mean(times),
        "median": statistics.median(times),
        "std": statistics.stdev(times) if n > 1 else 0.0,
        "min": times_sorted[0],
        "max": times_sorted[-1],
        "p10": times_sorted[max(0, int(n * 0.10) - 1)],
        "p90": times_sorted[min(n - 1, int(n * 0.90))],
    }


def paired_ci(
    a: list[float],
    b: list[float],
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int | None = None,
) -> tuple[float, float, float]:
    """Bootstrap CI for mean(a - b) where a[i] and b[i] are the same plane.
    Returns (mean_diff, ci_lower, ci_upper)."""
    diffs = [x - y for x, y in zip(a, b)]
    n = len(diffs)
    observed = statistics.mean(diffs)
    if n < 2:
        return observed, observed, observed
    rng = Rng(0 if seed is None else seed)
    boots = []
    for _ in range(n_boot):
        boots.append(sum(diffs[int(rng.random() * n)] for _ in range(n)) / n)
    boots.sort()
    return observed, boots[int(n_boot * alpha / 2)], boots[int(n_boot * (1 - alpha / 2)) - 1]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def compare(k: int, seed: int | None, debug: bool = False, **sim_kwargs) -> dict:
    """courtesy vs aisle_priority on the same K planes."""
    res = {name: run_experiment(py, k=k, seed=seed, debug=debug, **sim_kwargs)
           for name, py in POLICIES.items()}
    out = {}
    for metric in ("total", "mean_exit"):
        a = [r[metric] for r in res["courtesy"]]
        b = [r[metric] for r in res["aisle_priority"]]
        diff, lo, hi = paired_ci(a, b, seed=seed)
        out[metric] = {
            "courtesy": compute_stats(a),
            "aisle_priority": compute_stats(b),
            "diff": diff, "lo": lo, "hi": hi,
            "pct": diff / statistics.mean(b) * 100 if statistics.mean(b) else 0.0,
        }
    out["raw"] = res
    return out


def print_compare(cmp: dict) -> None:
    labels = {"total": "Total deboarding time", "mean_exit": "Mean time to exit per passenger"}
    for metric, label in labels.items():
        m = cmp[metric]
        c, a = m["courtesy"], m["aisle_priority"]
        print(f"\n{'=' * 62}\n  {label}\n{'=' * 62}")
        print(f"  {'':14s}{'courtesy':>14s}{'aisle_priority':>18s}")
        for key in ("mean", "median", "std", "p10", "p90"):
            print(f"  {key:14s}{c[key]:>12.1f} s{a[key]:>16.1f} s")
        print(f"  courtesy − aisle_priority (paired): {m['diff']:+.1f} s "
              f"[95% CI {m['lo']:+.1f}, {m['hi']:+.1f}]  ({m['pct']:+.1f}%)")


SWEEPS = {
    "p_prefetch": [0.0, 0.5, 0.8, 1.0],
    "p_bag": [0.2, 0.4, 0.65, 0.9],
    "p_occupancy": [0.5, 0.7, 0.85, 1.0],
    "door_headway": [1.0, 1.5, 2.0, 3.0],
    "bag_mode": [3, 5, 8, 12],
    "prep_mode": [0, 2, 5, 8],
    "cells_per_row": [1.0, 1.5, 2.0],
}


def _with_override(sim_kwargs: dict, param: str, value) -> dict:
    kw = dict(sim_kwargs)
    if param == "bag_mode":
        lo, hi, _ = kw["bag_range"]
        kw["bag_range"] = (lo, max(hi, value), value)
    elif param == "prep_mode":
        lo, hi, _ = kw["prep_range"]
        kw["prep_range"] = (lo, max(hi, value), value)
    else:
        kw[param] = value
    return kw


def sweep(k: int, seed: int | None, csv_path: str | None, **sim_kwargs) -> None:
    rows_out = []

    print(f"\n{'=' * 62}\n  Sweep over p_yield (same {k} planes each)\n{'=' * 62}")
    print(f"  {'p_yield':>8s}{'total mean':>14s}{'Δ vs 0':>10s}{'95% CI':>20s}{'mean exit':>12s}")
    base = None
    for py in [0.0, 0.1, 0.25, 0.5, 0.75, 1.0]:
        res = run_experiment(py, k=k, seed=seed, **sim_kwargs)
        tot = [r["total"] for r in res]
        if base is None:
            base = tot
        d, lo, hi = paired_ci(tot, base, seed=seed)
        mex = statistics.mean(r["mean_exit"] for r in res)
        print(f"  {py:8.2f}{statistics.mean(tot):12.1f} s{d:+9.1f}s  [{lo:+6.1f}, {hi:+6.1f}]{mex:10.1f} s")
        rows_out.append(["p_yield", py, py, statistics.mean(tot), d, lo, hi, mex])

    for param, values in SWEEPS.items():
        print(f"\n  Sensitivity to {param}: courtesy − aisle_priority (total time)")
        for v in values:
            kw = _with_override(sim_kwargs, param, v)
            cmp = compare(k, seed, **kw)
            m = cmp["total"]
            print(f"    {param}={v!s:<6}  courtesy {m['courtesy']['mean']:6.1f} s  "
                  f"aisle {m['aisle_priority']['mean']:6.1f} s  "
                  f"Δ {m['diff']:+6.1f} s [{m['lo']:+.1f}, {m['hi']:+.1f}]  ({m['pct']:+.1f}%)")
            rows_out.append([param, v, None, m["courtesy"]["mean"], m["diff"], m["lo"], m["hi"],
                             cmp["mean_exit"]["diff"]])

    if csv_path:
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sweep", "value", "p_yield", "mean_total", "diff", "ci_lo", "ci_hi",
                        "mean_exit_or_diff"])
            w.writerows(rows_out)
        print(f"\nResults saved to {csv_path}")


def add_model_args(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group("model")
    g.add_argument("--rows", type=int, default=30, help="Seat rows (default: 30)")
    g.add_argument("--p_occupancy", type=float, default=1.0, help="Seat occupancy probability (default: 1.0)")
    g.add_argument("--p_bag", type=float, default=0.65, help="Probability of an overhead bag (default: 0.65)")
    g.add_argument("--p_prefetch", type=float, default=0.8,
                   help="Probability a bag owner can take it while waiting at the seat (default: 0.8)")
    g.add_argument("--prep", type=float, nargs=3, default=[0, 10, 2], metavar=("MIN", "MAX", "MODE"),
                   help="Seated preparation time, triangular (default: 0 10 2)")
    g.add_argument("--bag", type=float, nargs=3, default=[2, 15, 5], metavar=("MIN", "MAX", "MODE"),
                   help="Overhead bag retrieval time, triangular (default: 2 15 5)")
    g.add_argument("--cells_per_row", type=float, default=1.5, help="Aisle cells per seat row (default: 1.5)")
    g.add_argument("--front_cells", type=int, default=2, help="Aisle cells between row 1 and the door (default: 2)")
    g.add_argument("--door_headway", type=float, default=1.0, help="Min seconds between exits at the door (default: 1.0)")
    g.add_argument("--gap_cells", type=int, default=2, help="How far back a seated passenger checks for walkers (default: 2)")


def sim_kwargs_from_args(args) -> dict:
    return dict(
        rows=args.rows,
        p_occupancy=args.p_occupancy,
        p_bag=args.p_bag,
        p_prefetch=args.p_prefetch,
        prep_range=(args.prep[0], args.prep[1], args.prep[2]),
        bag_range=(args.bag[0], args.bag[1], args.bag[2]),
        cells_per_row=args.cells_per_row,
        front_cells=args.front_cells,
        door_headway=args.door_headway,
        gap_cells=args.gap_cells,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Single-aisle deboarding: courtesy (p_yield=1) vs aisle priority (p_yield=0)."
    )
    parser.add_argument("mode", nargs="?", default="compare", choices=["compare", "sweep"])
    parser.add_argument("--k", type=int, default=500, help="Monte Carlo runs per configuration (default: 500)")
    parser.add_argument("--seed", type=int, default=None, help="Base seed (default: 0)")
    parser.add_argument("--debug", action="store_true", help="Trace the first courtesy and aisle_priority runs")
    parser.add_argument("--csv", type=str, default=None, help="Save results to CSV")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                        help="Worker processes (default: all CPUs)")
    add_model_args(parser)
    args = parser.parse_args()
    sim_kwargs = sim_kwargs_from_args(args)

    global _executor
    if args.jobs > 1:
        _executor = ProcessPoolExecutor(args.jobs)
    try:
        _main(args, sim_kwargs)
    finally:
        if _executor is not None:
            _executor.shutdown()
            _executor = None


def _main(args, sim_kwargs: dict) -> None:
    print("\nAirplane Deboarding Simulation")
    print(f"  Rows: {args.rows}  |  Occupancy: {args.p_occupancy:.0%}  |  "
          f"P(bag): {args.p_bag:.0%}  |  Door: {args.door_headway:g} s/pax  |  K: {args.k}")

    if args.mode == "sweep":
        sweep(args.k, args.seed, args.csv, **sim_kwargs)
        print()
        return

    cmp = compare(args.k, args.seed, debug=args.debug, **sim_kwargs)
    print_compare(cmp)
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run", "policy", "total", "mean_exit", "p90_exit"])
            for pol in POLICIES:
                for i, r in enumerate(cmp["raw"][pol]):
                    w.writerow([i, pol, r["total"], r["mean_exit"], r["p90_exit"]])
        print(f"\nResults saved to {args.csv}")
    print()


if __name__ == "__main__":
    main()
