#!/usr/bin/env python3
"""
=============================================================================
  Airplane Single-Aisle Deboarding Simulator
  Continuous agent-based model + Monte Carlo
=============================================================================

Question: is deboarding faster if people already standing in the aisle (bags
in hand) keep walking, and seated people wait for a real gap -- instead of
the usual courtesy where the aisle stops to let each row out?

The policy is a single behavioural parameter:

  p_yield = probability that a walking passenger stops to let a row out.
            1.0 -> "courtesy"        (today's etiquette)
            0.0 -> "aisle_priority"  (the hypothesis)

Usage examples:
  python simulate_deboarding.py                      # courtesy vs aisle_priority
  python simulate_deboarding.py --k 300 --seed 1
  python simulate_deboarding.py sweep --k 200 --seed 1 --csv sweep.csv
  python simulate_deboarding.py params               # every parameter + source
  python simulate_deboarding.py --set bag_length=0.4 --set p_yield=0.5

=============================================================================
Model.  Every number is a field of `Config` with its unit and where it comes
from; `params` lists them.  Distributions are quantile tables so measured
data can be pasted in directly.

1. Geometry.  The aisle is a 1-D line: x = metres from the door (x = 0).
   Row r's centre is at door_to_row1 + r * seat_pitch.  Seats A B C | D E F.

2. Passenger life cycle:
     SEATED      preparing while seated (belt, phone, jacket): `prep_time`.
     READY       done, but someone between them and the aisle is still in
                 the row (aisle seat leaves first, then middle, then window).
     TO_EDGE     standing up and shuffling sideways to the aisle edge:
                 stand_time + lateral_time per seat crossed.
     AT_EDGE     standing at the aisle edge inside the row, wanting to step
                 out.  A share `p_prefetch` of bag owners whose bin is within
                 reach take the bag down here while they wait.
     ENTERING    stepping into the aisle (occupies aisle space): step_in_time.
     BACKTRACK   bag stored behind, out of reach: walking against the flow
                 (see 5).
     RETRIEVING  taking the bag down in the aisle, blocking it: bag_time.
     WALKING     moving to the door (possibly stopped in the queue).  With a
                 bag in hand they are `bag_length` longer and walk slower.
     EXITED

3. Aisle movement (car-following, parallel update each tick of `dt`).  Each
   walker has a body depth, a desired speed and an acceleration limit, and
   keeps a standstill gap `s0` and a time headway `headway` to whoever is
   ahead.  A stopped walker only starts once the gap exceeds s0 + start_gap,
   so a jam restarts as a wave.  Nobody overtakes.

4. Bins.  The bag is in the bin `bin_offsets` rows from the seat (negative =
   towards the door).  Within `reach` it is taken in place; ahead, the walker
   stops there on the way out; behind (point 5).

5. Backtracking.  A passenger whose bag is behind, out of reach, waits at the
   aisle edge until the stretch of aisle from their row to the bin is empty
   and nobody is coming, reserves it, walks back, takes the bag and joins the
   flow.  Nobody enters a reserved stretch and walkers behind stop before it.

6. Yielding (the policy).  When a walker comes within `yield_distance` of a
   row where someone is getting out, it decides once, with probability
   `p_yield`, whether to stop and let that row out.  If it does, it waits
   behind the row until the row has nobody left trying to get out.

7. Gap acceptance (any policy).  Someone at the aisle edge may also step out
   without being let in, if there is room in their row's stretch of aisle and
   the nearest walker behind the row is farther than
   gap_distance + speed * gap_time from the row's back edge.

8. Door.  One passenger leaves every `door_headway` seconds at most.

9. Randomness.  A small portable PRNG (mulberry32) and only + - * / sqrt, so
   the web version (web/index.html) reproduces runs bit for bit.  Passenger
   generation and yield decisions use separate streams, so different p_yield
   values see the same passengers (paired comparisons).
=============================================================================
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import statistics
from concurrent.futures import Executor, ProcessPoolExecutor
from dataclasses import dataclass, field, fields, replace
from enum import IntEnum
from typing import Optional


# ---------------------------------------------------------------------------
# Portable RNG and sampling (bit-identical to the JS port)
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


Quantiles = tuple[tuple[float, float], ...]   # ((p, value), ...), p from 0 to 1


def sample_quantiles(knots: Quantiles, u: float) -> float:
    """Inverse CDF by linear interpolation between (probability, value) knots."""
    for i in range(1, len(knots)):
        p1, v1 = knots[i]
        if u < p1 or i == len(knots) - 1:
            p0, v0 = knots[i - 1]
            if p1 == p0:
                return v1
            return v0 + (u - p0) / (p1 - p0) * (v1 - v0)
    return knots[-1][1]


def sample_discrete(table: tuple[tuple[int, float], ...], u: float) -> int:
    """Pick a value from ((value, probability), ...)."""
    acc = 0.0
    for value, prob in table:
        acc += prob
        if u < acc:
            return value
    return table[-1][0]


def _to_ticks(x: float) -> int:
    # floor(x + 0.5) rather than round(): identical to JS Math.floor(x + 0.5)
    return int(math.floor(x + 0.5))


# ---------------------------------------------------------------------------
# Configuration: every parameter is a slot for measured data
# ---------------------------------------------------------------------------

def _p(default, unit: str, doc: str, source: str, measure: str = ""):
    return field(default=default, metadata={"unit": unit, "doc": doc, "source": source,
                                            "measure": measure})


EST = "estimate, no data yet"


@dataclass(frozen=True)
class Config:
    # -- cabin ---------------------------------------------------------------
    rows: int = _p(30, "rows", "Seat rows (3-3 layout)", "A320 typical")
    seat_pitch: float = _p(0.76, "m", "Distance between rows", "A320 typical, 29-30 in")
    door_to_row1: float = _p(2.0, "m", "Door L1 to the centre of row 1", EST,
                             "cabin drawing")
    # -- who is on board -----------------------------------------------------
    p_occupancy: float = _p(1.0, "prob", "Seat occupancy", "scenario")
    p_bag: float = _p(0.65, "prob", "Passengers with a bag in the overhead bin",
                      "Schultz 2023: 25/50/25 % carry 0/1/2 items (all kinds)",
                      "count bins opened per passenger")
    bin_offsets: tuple[tuple[int, float], ...] = _p(
        ((-3, 0.01), (-2, 0.03), (-1, 0.10), (0, 0.72), (1, 0.10), (2, 0.03), (3, 0.01)),
        "rows, prob", "Where the bag is relative to the seat row (negative = towards the door)",
        EST + "; Vink 2019 reports bags stored further back", "video: row of seat vs row of bin")
    p_prefetch: float = _p(0.8, "prob",
                           "Bag owners who take the bag down standing in their row if it is in reach",
                           "calibrated: courtesy gives 14-15 pax/min (observed 15-17, Wald 2014) and "
                           "12.7 min for a full A320 (Vink 2019: 6:27-13:00)",
                           "video: bag taken before or after stepping into the aisle")
    # -- bodies and walking --------------------------------------------------
    body_depth: Quantiles = _p(((0.0, 0.28), (0.5, 0.34), (1.0, 0.45)), "m",
                               "Front-to-back aisle length a standing person occupies", EST,
                               "video: people per metre in a stopped queue")
    bag_length: float = _p(0.25, "m", "Extra aisle length when carrying a bag", EST)
    walk_speed: Quantiles = _p(((0.0, 0.5), (0.5, 0.8), (1.0, 1.1)), "m/s",
                               "Free walking speed in the aisle",
                               "Schultz 2023: 0.8 m/s; CALM: 1.1-1.3 m/s general walking",
                               "video: time to walk N rows with nobody ahead")
    bag_speed_factor: float = _p(0.85, "x", "Speed multiplier when carrying a bag", EST)
    back_speed: float = _p(0.5, "m/s", "Speed walking back against the flow to a bin", EST)
    accel: float = _p(1.0, "m/s2", "Acceleration limit", EST)
    s0: float = _p(0.15, "m", "Standstill gap between people in the queue", EST,
                   "video: gap between stopped people")
    headway: float = _p(0.6, "s", "Time headway kept while following", EST,
                        "video: queue discharge rate")
    start_gap: float = _p(0.25, "m", "Extra gap a stopped person waits for before moving", EST,
                          "video: delay of the restart wave per person")
    reach: float = _p(0.9, "m", "How far along the aisle a person can reach a bin", EST)
    # -- times (seconds) -----------------------------------------------------
    prep_time: Quantiles = _p(
        ((0.0, 0.0), (0.1, 1.4), (0.25, 2.3), (0.5, 3.7), (0.75, 5.5), (0.9, 7.2), (1.0, 10.0)),
        "s", "Seated preparation after the seatbelt sign goes off", EST,
        "video: seatbelt sign off -> starts standing")
    stand_time: Quantiles = _p(((0.0, 0.8), (0.5, 1.5), (1.0, 3.0)), "s", "Time to stand up", EST)
    lateral_time: Quantiles = _p(((0.0, 0.8), (0.5, 1.2), (1.0, 2.0)), "s",
                                 "Time to shuffle past one seat towards the aisle", EST)
    step_in_time: Quantiles = _p(((0.0, 0.5), (0.5, 1.0), (1.0, 2.0)), "s",
                                 "Time to step from the row into the aisle", EST)
    bag_time: Quantiles = _p(
        ((0.0, 1.5), (0.05, 2.0), (0.1, 2.6), (0.25, 4.3), (0.5, 7.3), (0.75, 10.9),
         (0.9, 14.7), (0.95, 17.2), (0.99, 22.1), (1.0, 30.0)),
        "s", "Time to take a bag down from the bin",
        "Weibull(1.7, 9 s), shape from Schultz 2023 storage times (1.7, 16 s); CALM uses 5-12 s",
        "video: bin opened -> bag on the floor")
    door_headway: float = _p(1.0, "s", "Minimum time between two passengers at the door", EST,
                             "video: exits per minute when the queue at the door is continuous")
    # -- behaviour -----------------------------------------------------------
    p_yield: float = _p(1.0, "prob", "Probability a walker stops to let a row out (the policy)",
                        "policy under study", "video: share of rows let out by the aisle")
    yield_distance: float = _p(0.8, "m", "How close to a row a walker decides whether to yield", EST)
    gap_distance: float = _p(1.0, "m", "Minimum free aisle behind to step out without being let in",
                             EST, "video: gaps accepted / rejected by people stepping out")
    gap_time: float = _p(1.5, "s", "Extra margin per m/s of the approaching walker", EST)
    # -- simulation ----------------------------------------------------------
    dt: float = _p(0.25, "s", "Time step", "numerical")


def config_to_dict(cfg: Config) -> dict:
    out = {}
    for f in fields(cfg):
        v = getattr(cfg, f.name)
        out[f.name] = [list(x) for x in v] if isinstance(v, tuple) else v
    return out


def parse_override(cfg: Config, text: str) -> Config:
    """'name=value' (value as JSON) -> new Config."""
    name, _, raw = text.partition("=")
    name = name.strip()
    if name not in {f.name for f in fields(cfg)}:
        raise SystemExit(f"unknown parameter {name!r}; see `params`")
    value = json.loads(raw)
    if isinstance(value, list):
        value = tuple(tuple(x) for x in value)
    return replace(cfg, **{name: value})


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class PaxState(IntEnum):
    SEATED = 0
    READY = 1
    TO_EDGE = 2
    AT_EDGE = 3
    ENTERING = 4
    BACKTRACK = 5
    RETRIEVING = 6
    WALKING = 7
    EXITED = 8


# Columns: A B C | aisle | D E F
COLUMN_AISLE_DISTANCE = {"C": 0, "D": 0, "B": 1, "E": 1, "A": 2, "F": 2}
LEFT_HALF = {"A", "B", "C"}
BLOCKING_COLUMNS = {
    "A": ["B", "C"],
    "B": ["C"],
    "C": [],
    "D": [],
    "E": ["D"],
    "F": ["D", "E"],
}
IN_AISLE = (PaxState.ENTERING, PaxState.BACKTRACK, PaxState.RETRIEVING, PaxState.WALKING)
OUT_OF_ROW = IN_AISLE + (PaxState.EXITED,)

POLICIES = {"courtesy": 1.0, "aisle_priority": 0.0}


def _x(p) -> float:
    return p.x


@dataclass(eq=False)
class Passenger:
    pid: int
    row: int            # 0-indexed (row 0 = front)
    col: str            # A..F
    has_bag: bool
    bin_row: int        # row of the bin holding the bag (== row if no bag)
    prefetch: bool      # would take the bag down in the row, if in reach
    prep: int           # ticks
    to_edge: int        # ticks: stand up + shuffle to the aisle edge
    step_in: int        # ticks
    bag: int            # ticks (0 if no bag)
    v0: float           # free walking speed, m/s
    depth: float        # body depth, m
    state: PaxState = PaxState.SEATED
    prep_left: int = 0
    edge_left: int = 0
    enter_left: int = 0
    bag_left: int = 0
    carrying: bool = False
    x: float = -1.0     # aisle position (m from the door) while in the aisle
    v: float = 0.0
    length: float = 0.0
    yield_row: Optional[int] = None         # row this walker is holding back for
    yields: dict[int, bool] = field(default_factory=dict)  # row -> decision
    blockers: list["Passenger"] = field(default_factory=list)
    t_ready: int = -1
    t_edge: int = -1
    t_enter: int = -1
    t_exit: int = -1

    def __post_init__(self):
        self.prep_left = self.prep
        self.bag_left = self.bag
        self.length = self.depth

    @property
    def is_left(self) -> bool:
        return self.col in LEFT_HALF


@dataclass
class Reservation:
    owner: Passenger
    a: float            # front end (towards the door)
    b: float            # back end


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class Simulator:
    """Tick-based continuous-aisle deboarding simulator."""

    def __init__(self, config: Config | None = None, seed: int = 0,
                 manifest: list[dict] | None = None, debug: bool = False, **overrides):
        self.cfg = cfg = replace(config or Config(), **overrides)
        self.debug = debug
        self.dt = cfg.dt
        self.gen_rng = Rng(seed)
        self.dyn_rng = Rng(seed ^ 0x9E3779B9)

        self.tick = 0
        self.last_exit = -math.inf
        self.door_ticks = max(1, self._ticks(cfg.door_headway))
        # nobody is longer than this, so neighbour searches can stop here
        self.near = cfg.body_depth[-1][1] + cfg.bag_length + cfg.s0
        self.aisle: list[Passenger] = []            # sorted by x, front first
        self.reservations: list[Reservation] = []
        self._held_rows: set[int] = set()           # rows a walker is holding back for

        self.passengers: list[Passenger] = []
        self._pax_by_row_col: dict[tuple[int, str], Passenger] = {}
        self.seats_by_row: list[list[Passenger]] = [[] for _ in range(cfg.rows)]
        if manifest is None:
            self._generate_passengers()
        else:
            for spec in manifest:
                self.add_passenger(**spec)
        self._link_blockers()
        self.n_inside = len(self.passengers)

    # -- Setup ---------------------------------------------------------------

    def _ticks(self, seconds: float) -> int:
        return _to_ticks(seconds / self.dt)

    def row_x(self, r: int) -> float:
        return self.cfg.door_to_row1 + r * self.cfg.seat_pitch

    def _generate_passengers(self) -> None:
        cfg, rng = self.cfg, self.gen_rng
        for row in range(cfg.rows):
            for col in "ABCDEF":
                if rng.random() >= cfg.p_occupancy:
                    continue
                has_bag = rng.random() < cfg.p_bag
                prep = sample_quantiles(cfg.prep_time, rng.random())
                stand = sample_quantiles(cfg.stand_time, rng.random())
                lateral = sample_quantiles(cfg.lateral_time, rng.random())
                step_in = sample_quantiles(cfg.step_in_time, rng.random())
                v0 = sample_quantiles(cfg.walk_speed, rng.random())
                depth = sample_quantiles(cfg.body_depth, rng.random())
                bag = 0.0
                offset = 0
                prefetch = False
                if has_bag:
                    bag = sample_quantiles(cfg.bag_time, rng.random())
                    offset = sample_discrete(cfg.bin_offsets, rng.random())
                    prefetch = rng.random() < cfg.p_prefetch
                self.add_passenger(
                    row=row, col=col, has_bag=has_bag, prep=prep, stand=stand,
                    lateral=lateral, step_in=step_in, v0=v0, depth=depth, bag=bag,
                    bin_offset=offset, prefetch=prefetch,
                )

    def add_passenger(
        self, row: int, col: str, has_bag: bool = False, prep: float = 0.0,
        stand: float = 1.0, lateral: float = 1.0, step_in: float = 1.0,
        v0: float = 0.8, depth: float = 0.34, bag: float = 0.0,
        bin_offset: int = 0, prefetch: bool = False,
    ) -> Passenger:
        """Add a passenger; times in seconds (converted to whole ticks)."""
        dist = COLUMN_AISLE_DISTANCE[col]
        bin_row = min(max(row + bin_offset, 0), self.cfg.rows - 1) if has_bag else row
        pax = Passenger(
            pid=len(self.passengers), row=row, col=col, has_bag=has_bag,
            bin_row=bin_row, prefetch=prefetch and has_bag,
            prep=self._ticks(prep),
            to_edge=max(1, self._ticks(stand + lateral * dist)),
            step_in=max(1, self._ticks(step_in)),
            bag=max(1, self._ticks(bag)) if has_bag else 0,
            v0=v0, depth=depth,
        )
        self.passengers.append(pax)
        self._pax_by_row_col[(row, col)] = pax
        self.seats_by_row[row].append(pax)
        self.seats_by_row[row].sort(key=lambda p: "ABCDEF".index(p.col))
        self.n_inside = len(self.passengers)
        return pax

    def _link_blockers(self) -> None:
        for p in self.passengers:
            p.blockers = [b for c in BLOCKING_COLUMNS[p.col]
                          if (b := self._pax_by_row_col.get((p.row, c))) is not None]

    # -- Geometry helpers ----------------------------------------------------

    def bin_x(self, p: Passenger) -> float:
        return self.row_x(p.bin_row)

    def needs_backtrack(self, p: Passenger) -> bool:
        """Bag stored behind the row, out of reach from it."""
        return p.has_bag and self.bin_x(p) - self.row_x(p.row) > self.cfg.reach

    def _bag_pending(self, p: Passenger) -> bool:
        return p.has_bag and not p.carrying

    def _leaving_forward(self, p: Passenger) -> bool:
        """Getting out of the row and will step out towards the door.  Someone
        who must walk back for their bag waits for an empty aisle instead, so
        nobody holds back for them (that would block the aisle they need)."""
        if p.state in (PaxState.TO_EDGE, PaxState.AT_EDGE):
            return not (self._bag_pending(p) and self.needs_backtrack(p))
        return p.state == PaxState.ENTERING

    def _row_wants_out(self, r: int) -> bool:
        """Someone in row r is visibly getting out."""
        for p in self.seats_by_row[r]:
            if p.state in (PaxState.TO_EDGE, PaxState.AT_EDGE):
                if self._leaving_forward(p):
                    return True
            elif p.state == PaxState.READY:
                for b in p.blockers:
                    if self._leaving_forward(b):
                        return True
        return False

    # -- Tick phases ---------------------------------------------------------

    def _phase_timers(self) -> None:
        cfg = self.cfg
        for p in self.passengers:
            st = p.state
            if st == PaxState.SEATED:
                if p.prep_left > 0:
                    p.prep_left -= 1
                if p.prep_left == 0:
                    p.state = st = PaxState.READY
                    p.t_ready = self.tick
            if st == PaxState.READY:
                if all(b.state in OUT_OF_ROW for b in p.blockers):
                    p.state = PaxState.TO_EDGE
                    p.edge_left = p.to_edge
            elif st == PaxState.TO_EDGE:
                p.edge_left -= 1
                if p.edge_left <= 0:
                    p.state = PaxState.AT_EDGE
                    p.t_edge = self.tick
            elif st == PaxState.AT_EDGE:
                if (p.prefetch and p.bag_left > 0 and not self.needs_backtrack(p)
                        and abs(self.bin_x(p) - self.row_x(p.row)) <= cfg.reach):
                    p.bag_left -= 1
                    if p.bag_left == 0:
                        p.carrying = True
                        p.length = p.depth + cfg.bag_length
            elif st == PaxState.ENTERING:
                p.enter_left -= 1
                if p.enter_left <= 0:
                    self._after_entering(p)
            elif st == PaxState.RETRIEVING:
                if p.bag_left > 0:
                    p.bag_left -= 1
                if p.bag_left == 0:
                    self._try_finish_bag(p)

    def _after_entering(self, p: Passenger) -> None:
        p.v = 0.0
        if not self._bag_pending(p):
            p.state = PaxState.WALKING
        elif self.needs_backtrack(p):
            p.state = PaxState.BACKTRACK
        elif self.bin_x(p) < p.x - self.cfg.reach:
            p.state = PaxState.WALKING          # stops at the bin on the way out
        else:
            p.state = PaxState.RETRIEVING

    def _try_finish_bag(self, p: Passenger) -> None:
        """Bag is down: put it in the aisle in front of you if there is room."""
        cfg = self.cfg
        k = self.aisle.index(p)
        front = p.x - p.length / 2
        room = math.inf
        if k > 0:
            lead = self.aisle[k - 1]
            room = front - (lead.x + lead.length / 2)
        for res in self.reservations:
            if res.owner is not p and res.b <= front:
                room = min(room, front - res.b)
        if room < cfg.bag_length + cfg.s0:
            return
        p.x = p.x - cfg.bag_length / 2
        p.length = p.length + cfg.bag_length
        p.carrying = True
        p.state = PaxState.WALKING
        p.v = 0.0
        self.reservations = [r for r in self.reservations if r.owner is not p]

    def _phase_door(self) -> None:
        if not self.aisle:
            return
        p = self.aisle[0]
        if (p.state == PaxState.WALKING and p.x <= 0.0
                and self.tick - self.last_exit >= self.door_ticks):
            p.state = PaxState.EXITED
            p.t_exit = self.tick
            p.v = 0.0
            self.aisle.pop(0)
            self.last_exit = self.tick
            self.n_inside -= 1
            if self.debug:
                print(f"  [EXIT] {p.row + 1}{p.col} (pid {p.pid})")

    def _phase_move(self) -> None:
        cfg = self.cfg
        dt = self.dt
        old_x = [p.x for p in self.aisle]
        wants: dict[int, bool] = {}
        self._held_rows.clear()

        def row_wants(r: int) -> bool:
            if r not in wants:
                wants[r] = self._row_wants_out(r)
            return wants[r]

        for k, p in enumerate(self.aisle):
            if p.state == PaxState.BACKTRACK:
                self._move_back(k, p, old_x)
                continue
            if p.state != PaxState.WALKING:
                p.v = 0.0
                continue

            front = p.x - p.length / 2
            gap = math.inf
            if k > 0:
                lead = self.aisle[k - 1]
                gap = front - (old_x[k - 1] + lead.length / 2)
            for res in self.reservations:
                if res.b <= front:
                    gap = min(gap, front - res.b)

            # yielding
            if p.yield_row is not None and not row_wants(p.yield_row):
                p.yield_row = None
            if p.yield_row is None:
                r = math.floor((front - cfg.door_to_row1 - cfg.seat_pitch / 2) / cfg.seat_pitch)
                if 0 <= r < cfg.rows:
                    back_edge = cfg.door_to_row1 + r * cfg.seat_pitch + cfg.seat_pitch / 2
                    if front - back_edge <= cfg.yield_distance and row_wants(r):
                        decision = p.yields.get(r)
                        if decision is None:
                            decision = self.dyn_rng.random() < cfg.p_yield
                            p.yields[r] = decision
                        if decision:
                            p.yield_row = r
                            if self.debug:
                                print(f"  [YIELD] {p.row + 1}{p.col} lets row {r + 1} out")
            if p.yield_row is not None:
                self._held_rows.add(p.yield_row)
                back_edge = (cfg.door_to_row1 + p.yield_row * cfg.seat_pitch
                             + cfg.seat_pitch / 2)
                gap = min(gap, front - back_edge)

            # speed
            v0 = p.v0 * cfg.bag_speed_factor if p.carrying else p.v0
            if p.v == 0.0 and gap - cfg.s0 < cfg.start_gap:
                v_new = 0.0
            else:
                v_new = min(v0, p.v + cfg.accel * dt, (gap - cfg.s0) / cfg.headway)
                if v_new < 0.0:
                    v_new = 0.0
            travel = v_new * dt

            # stop targets: the door, or a bin ahead
            target = 0.0
            at_bin = False
            if self._bag_pending(p):
                target = self.bin_x(p) + cfg.reach / 2
                at_bin = True
            limit = p.x - target
            if travel >= limit:
                travel = limit if limit > 0.0 else 0.0
                p.x = target if limit > 0.0 else p.x
                if at_bin:
                    p.state = PaxState.RETRIEVING
            else:
                p.x = p.x - travel
            p.v = travel / dt

    def _move_back(self, k: int, p: Passenger, old_x: list[float]) -> None:
        cfg = self.cfg
        target = self.bin_x(p) - cfg.reach / 2
        step = cfg.back_speed * self.dt
        if k + 1 < len(self.aisle):
            f = self.aisle[k + 1]
            room = (old_x[k + 1] - f.length / 2) - (p.x + p.length / 2) - cfg.s0
            if room < step:
                step = room
        if step < 0.0:
            step = 0.0
        if step >= target - p.x:
            p.x = target
            p.state = PaxState.RETRIEVING
        else:
            p.x = p.x + step
        p.v = 0.0

    # -- Entering the aisle --------------------------------------------------

    def _free_spot(self, r: int, length: float) -> Optional[float]:
        """Centre position in row r's stretch of aisle with room for `length`,
        closest to the row centre (ties towards the door).  None if full."""
        cfg = self.cfg
        xr = self.row_x(r)
        lo = xr - cfg.seat_pitch / 2
        hi = xr + cfg.seat_pitch / 2
        forbidden = []
        # the aisle is sorted by x, so only neighbours within `near` of the
        # row's stretch can get in the way
        k = bisect.bisect_left(self.aisle, lo - self.near, key=_x)
        while k < len(self.aisle) and self.aisle[k].x < hi + self.near:
            q = self.aisle[k]
            k += 1
            half = (q.length + length) / 2 + cfg.s0
            a, b = q.x - half, q.x + half
            if b > lo and a < hi:
                forbidden.append((a, b))
        for res in self.reservations:
            a, b = res.a - length / 2 - cfg.s0, res.b + length / 2 + cfg.s0
            if b > lo and a < hi:
                forbidden.append((a, b))
        # the row centre is the best possible spot; otherwise the closest free
        # edge of a forbidden interval (ties towards the door)
        if not any(a < xr < b for a, b in forbidden):
            return xr
        cands = []
        for a, b in forbidden:
            cands.append(a)
            cands.append(b)
        best = None
        for c in cands:
            if c < lo or c > hi:
                continue
            blocked = False
            for a, b in forbidden:
                if a < c < b:
                    blocked = True
                    break
            if blocked:
                continue
            if (best is None or abs(c - xr) < abs(best - xr)
                    or (abs(c - xr) == abs(best - xr) and c < best)):
                best = c
        return best

    def _gap_ok(self, back_edge: float, r: int) -> bool:
        """Nobody walking towards `back_edge` from behind within the gap."""
        cfg = self.cfg
        k = bisect.bisect_right(self.aisle, back_edge, key=_x)
        if k == len(self.aisle):
            return True
        q = self.aisle[k]
        if q.state != PaxState.WALKING or q.yield_row == r:
            return True
        dist = (q.x - q.length / 2) - back_edge
        return dist >= cfg.gap_distance + q.v * cfg.gap_time

    def _segment_free(self, lo: float, hi: float) -> bool:
        s0 = self.cfg.s0
        for q in self.aisle:
            if q.x + q.length / 2 + s0 > lo and q.x - q.length / 2 - s0 < hi:
                return False
        for res in self.reservations:
            if res.b > lo and res.a < hi:
                return False
        return True

    def _try_enter(self, p: Passenger, r: int) -> bool:
        cfg = self.cfg
        if self._bag_pending(p) and self.needs_backtrack(p):
            b = (self.bin_x(p) - cfg.reach / 2) + p.length / 2 + cfg.s0
            if not self._gap_ok(b, r):
                return False
            spot = self._free_spot(r, p.length)
            if spot is None or not self._segment_free(spot - p.length / 2, b):
                return False
            self.reservations.append(Reservation(owner=p, a=spot - p.length / 2, b=b))
        else:
            offered = r in self._held_rows
            if not offered and not self._gap_ok(self.row_x(r) + cfg.seat_pitch / 2, r):
                return False
            spot = self._free_spot(r, p.length)
            if spot is None:
                return False
        p.state = PaxState.ENTERING
        p.enter_left = p.step_in
        p.t_enter = self.tick
        p.x = spot
        p.v = 0.0
        self.aisle.insert(bisect.bisect_left(self.aisle, spot, key=_x), p)
        if self.debug:
            print(f"  [ENTER] {p.row + 1}{p.col} at x={spot:.2f}")
        return True

    def _phase_enter(self) -> None:
        for r in range(self.cfg.rows):
            cands = [p for p in self.seats_by_row[r] if p.state == PaxState.AT_EDGE]
            if not cands:
                continue
            left_first = (r + self.tick) % 2 == 0
            cands.sort(key=lambda p: (p.t_edge, p.is_left != left_first))
            for p in cands:
                self._try_enter(p, r)

    # -- Main loop -----------------------------------------------------------

    def step(self) -> bool:
        """Execute one tick.  Returns True while someone is still inside."""
        self.tick += 1
        if self.debug:
            print(f"\n--- t={self.time:g}s ---")
        self._phase_timers()
        self._phase_door()
        self._phase_move()
        self._phase_enter()
        if self.debug:
            self._print_state()
        return self.n_inside > 0

    @property
    def time(self) -> float:
        return self.tick * self.dt

    def run(self, max_ticks: int = 200000) -> float:
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
        glyph = {PaxState.ENTERING: "E", PaxState.RETRIEVING: "B",
                 PaxState.BACKTRACK: "<", PaxState.WALKING: ">"}
        cells = " ".join(f"{glyph[p.state]}{p.x:.1f}" for p in self.aisle)
        print(f"  door| {cells} |  inside={self.n_inside}")


# ---------------------------------------------------------------------------
# Experiment runner
# ---------------------------------------------------------------------------

def run_seeds(k: int, seed: int | None) -> list[int]:
    base = Rng(0 if seed is None else seed)
    return [base.next_u32() for _ in range(k)]


_executor: Executor | None = None   # set by main() to run simulations in parallel


def _run_one(job: tuple[Config, int, bool]) -> dict:
    cfg, seed, debug = job
    sim = Simulator(cfg, seed=seed, debug=debug)
    sim.run()
    return sim.metrics()


def run_experiment(
    p_yield: float,
    k: int = 1000,
    seed: int | None = None,
    debug: bool = False,
    config: Config | None = None,
    **overrides,
) -> list[dict]:
    """Run K simulations.  The same seed gives the same K planes for any
    p_yield, so results from two calls can be compared run by run."""
    cfg = replace(config or Config(), p_yield=p_yield, **overrides)
    jobs = [(cfg, s, debug and i == 0) for i, s in enumerate(run_seeds(k, seed))]
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

def compare(k: int, seed: int | None, config: Config, debug: bool = False) -> dict:
    """courtesy vs aisle_priority on the same K planes."""
    res = {name: run_experiment(py, k=k, seed=seed, debug=debug, config=config)
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


def print_compare(cmp: dict, n_pax: float) -> None:
    labels = {"total": "Total deboarding time", "mean_exit": "Mean time to exit per passenger"}
    for metric, label in labels.items():
        m = cmp[metric]
        c, a = m["courtesy"], m["aisle_priority"]
        print(f"\n{'=' * 62}\n  {label}\n{'=' * 62}")
        print(f"  {'':14s}{'courtesy':>14s}{'aisle_priority':>18s}")
        for key in ("mean", "median", "std", "p10", "p90"):
            print(f"  {key:14s}{c[key]:>12.1f} s{a[key]:>16.1f} s")
        if metric == "total" and n_pax:
            print(f"  {'pax/min':14s}{n_pax / c['mean'] * 60:>14.1f}{n_pax / a['mean'] * 60:>18.1f}")
        print(f"  courtesy − aisle_priority (paired): {m['diff']:+.1f} s "
              f"[95% CI {m['lo']:+.1f}, {m['hi']:+.1f}]  ({m['pct']:+.1f}%)")


SWEEPS = {
    "p_prefetch": [0.0, 0.5, 0.8, 1.0],
    "p_bag": [0.2, 0.4, 0.65, 0.9],
    "p_occupancy": [0.5, 0.7, 0.85, 1.0],
    "door_headway": [1.0, 1.5, 2.0, 3.0],
    "bag_scale": [0.5, 1.0, 1.5, 2.0],
    "bins_elsewhere": [0.0, 0.28, 0.5],
    "gap_distance": [0.5, 1.0, 2.0],
}


def _with_override(cfg: Config, param: str, value) -> Config:
    if param == "bag_scale":
        return replace(cfg, bag_time=tuple((p, v * value) for p, v in cfg.bag_time))
    if param == "bins_elsewhere":
        # share of bags NOT above the own row, spread like the default table
        others = [(o, w) for o, w in cfg.bin_offsets if o != 0]
        total = sum(w for _, w in others)
        table = tuple((o, (1 - value) if o == 0 else w / total * value)
                      for o, w in cfg.bin_offsets)
        return replace(cfg, bin_offsets=table)
    return replace(cfg, **{param: value})


def sweep(k: int, seed: int | None, csv_path: str | None, config: Config) -> None:
    rows_out = []

    print(f"\n{'=' * 62}\n  Sweep over p_yield (same {k} planes each)\n{'=' * 62}")
    print(f"  {'p_yield':>8s}{'total mean':>14s}{'Δ vs 0':>10s}{'95% CI':>20s}{'mean exit':>12s}")
    base = None
    for py in [0.0, 0.1, 0.25, 0.5, 0.75, 1.0]:
        res = run_experiment(py, k=k, seed=seed, config=config)
        tot = [r["total"] for r in res]
        if base is None:
            base = tot
        d, lo, hi = paired_ci(tot, base, seed=seed)
        mex = statistics.mean(r["mean_exit"] for r in res)
        print(f"  {py:8.2f}{statistics.mean(tot):12.1f} s{d:+9.1f}s  [{lo:+6.1f}, {hi:+6.1f}]{mex:10.1f} s",
              flush=True)
        rows_out.append(["p_yield", py, py, statistics.mean(tot), d, lo, hi, mex])

    for param, values in SWEEPS.items():
        print(f"\n  Sensitivity to {param}: courtesy − aisle_priority (total time)", flush=True)
        for v in values:
            cmp = compare(k, seed, _with_override(config, param, v))
            m = cmp["total"]
            print(f"    {param}={v!s:<6}  courtesy {m['courtesy']['mean']:6.1f} s  "
                  f"aisle {m['aisle_priority']['mean']:6.1f} s  "
                  f"Δ {m['diff']:+6.1f} s [{m['lo']:+.1f}, {m['hi']:+.1f}]  ({m['pct']:+.1f}%)",
                  flush=True)
            rows_out.append([param, v, None, m["courtesy"]["mean"], m["diff"], m["lo"], m["hi"],
                             cmp["mean_exit"]["diff"]])

    if csv_path:
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sweep", "value", "p_yield", "mean_total", "diff", "ci_lo", "ci_hi",
                        "mean_exit_or_diff"])
            w.writerows(rows_out)
        print(f"\nResults saved to {csv_path}")


def print_params(cfg: Config) -> None:
    """Every model parameter, its value, and where the value comes from."""
    for f in fields(cfg):
        md = f.metadata
        value = getattr(cfg, f.name)
        if isinstance(value, tuple):
            value = " ".join(f"{a:g}:{b:g}" for a, b in value)
        print(f"{f.name:16s} {value!s:<30} [{md['unit']}]  {md['doc']}")
        print(f"{'':16s} source: {md['source']}")
        if md["measure"]:
            print(f"{'':16s} measure: {md['measure']}")


def add_model_args(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group("model")
    g.add_argument("--rows", type=int, default=None, help="Seat rows (default: 30)")
    g.add_argument("--p_occupancy", type=float, default=None, help="Seat occupancy (default: 1.0)")
    g.add_argument("--p_bag", type=float, default=None, help="Share with an overhead bag (default: 0.65)")
    g.add_argument("--p_prefetch", type=float, default=None,
                   help="Share taking the bag down in the row (default: 0.8)")
    g.add_argument("--door_headway", type=float, default=None, help="Seconds between exits (default: 1.0)")
    g.add_argument("--config", metavar="FILE",
                   help="JSON file of parameter overrides, e.g. written by calibrate.py")
    g.add_argument("--set", action="append", default=[], metavar="NAME=JSON",
                   help="Override any parameter, e.g. --set bag_length=0.4 (see `params`)")


def load_config(path: str, base: Config | None = None) -> Config:
    """Apply a JSON object of parameter overrides to `base`."""
    cfg = base or Config()
    with open(path) as f:
        for name, value in json.load(f).items():
            cfg = parse_override(cfg, f"{name}={json.dumps(value)}")
    return cfg


def config_from_args(args) -> Config:
    cfg = load_config(args.config) if args.config else Config()
    for name in ("rows", "p_occupancy", "p_bag", "p_prefetch", "door_headway"):
        value = getattr(args, name)
        if value is not None:
            cfg = replace(cfg, **{name: value})
    for text in args.set:
        cfg = parse_override(cfg, text)
    return cfg


def main():
    parser = argparse.ArgumentParser(
        description="Single-aisle deboarding: courtesy (p_yield=1) vs aisle priority (p_yield=0)."
    )
    parser.add_argument("mode", nargs="?", default="compare", choices=["compare", "sweep", "params"])
    parser.add_argument("--k", type=int, default=300, help="Monte Carlo runs per configuration (default: 300)")
    parser.add_argument("--seed", type=int, default=None, help="Base seed (default: 0)")
    parser.add_argument("--debug", action="store_true", help="Trace the first run of each policy")
    parser.add_argument("--csv", type=str, default=None, help="Save results to CSV")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                        help="Worker processes (default: all CPUs)")
    add_model_args(parser)
    args = parser.parse_args()
    cfg = config_from_args(args)

    if args.mode == "params":
        print_params(cfg)
        return

    global _executor
    if args.jobs > 1:
        _executor = ProcessPoolExecutor(args.jobs)
    try:
        _main(args, cfg)
    finally:
        if _executor is not None:
            _executor.shutdown()
            _executor = None


def _main(args, cfg: Config) -> None:
    n_pax = cfg.rows * 6 * cfg.p_occupancy
    print("\nAirplane Deboarding Simulation (continuous aisle)")
    print(f"  Rows: {cfg.rows}  |  Occupancy: {cfg.p_occupancy:.0%}  |  P(bag): {cfg.p_bag:.0%}  |  "
          f"Prefetch: {cfg.p_prefetch:.0%}  |  Door: {cfg.door_headway:g} s/pax  |  K: {args.k}")

    if args.mode == "sweep":
        sweep(args.k, args.seed, args.csv, cfg)
        print()
        return

    cmp = compare(args.k, args.seed, cfg, debug=args.debug)
    print_compare(cmp, n_pax)
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
