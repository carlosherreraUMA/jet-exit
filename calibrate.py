#!/usr/bin/env python3
"""
=============================================================================
  Calibrate the deboarding model from annotated videos
=============================================================================

Turns one or more annotation CSVs (made with web/annotate.html, or by hand)
into model parameters, and checks the calibrated model against the observed
exits.

Usage:
  python calibrate.py flight1.csv flight2.csv --out calibrated.json
  python calibrate.py flights/*.csv --out calibrated.json --validate
  python simulate_deboarding.py --config calibrated.json

  # check the pipeline itself: annotate a simulated flight whose parameters
  # are known, then see how well they are recovered
  python calibrate.py --synthetic 3 --out synthetic.json

Annotation format (CSV, one row per event, times in real seconds):

  video,t,who,event,value,note
  lhr-mad-01,0.0,,belt_off,,
  lhr-mad-01,4.2,12C,stand,,
  lhr-mad-01,6.0,12C,edge,,
  lhr-mad-01,6.9,12C,step_in,,
  lhr-mad-01,6.9,12C,let_in,,
  lhr-mad-01,8.1,12C,bin_open,13,
  lhr-mad-01,15.3,12C,bag_down,,
  lhr-mad-01,16.0,12C,walk,,
  lhr-mad-01,61.4,12C,exit,,
  lhr-mad-01,62.5,,door,,
  lhr-mad-01,0,,meta,rows=30,

  who     seat label: row number + letter ("12C").  Empty for events that
          belong to the whole flight (belt_off, door, meta).
  event   belt_off  seatbelt sign goes off (time zero of the flight)
          stand     starts standing up (from the seat)
          edge      standing in the row next to the aisle, ready to step out
          step_in   first foot in the aisle
          let_in    someone walking in the aisle stopped to let them out
          no_let_in someone was coming and did not stop (they waited or cut in)
          bin_open  reaches into / opens the overhead bin; value = bin row
          bag_down  bag on the floor or in hand, ready to go
          back      starts walking back against the flow to reach a bin
          walk      starts walking towards the door
          exit      passes the door
          door      someone (unidentified) passes the door
          meta      value = "key=value" (aircraft, rows, camera, speed...)
=============================================================================
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field, fields, replace

from simulate_deboarding import (
    COLUMN_AISLE_DISTANCE, Config, PaxState, Simulator, config_to_dict, run_experiment,
)

EVENTS = {"belt_off", "stand", "edge", "step_in", "let_in", "no_let_in", "bin_open",
          "bag_down", "back", "walk", "exit", "door", "meta"}
QUANTILE_PS = (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0)


# ---------------------------------------------------------------------------
# Reading annotations
# ---------------------------------------------------------------------------

@dataclass
class Person:
    who: str
    row: int                 # 0-indexed
    col: str
    events: dict[str, float] = field(default_factory=dict)   # first time of each event
    bin_row: int | None = None


@dataclass
class Flight:
    video: str
    meta: dict[str, str] = field(default_factory=dict)
    belt_off: float | None = None
    people: dict[str, Person] = field(default_factory=dict)
    exits: list[float] = field(default_factory=list)          # every door passage


def parse_seat(who: str) -> tuple[int, str]:
    who = who.strip().upper()
    return int(who[:-1]) - 1, who[-1]


def read_flights(paths: list[str]) -> list[Flight]:
    flights: dict[str, Flight] = {}
    for path in paths:
        with open(path, newline="") as f:
            for n, r in enumerate(csv.DictReader(f), start=2):
                ev = (r.get("event") or "").strip()
                if ev not in EVENTS:
                    raise SystemExit(f"{path}:{n}: unknown event {ev!r}")
                fl = flights.setdefault(r["video"], Flight(video=r["video"]))
                who = (r.get("who") or "").strip().upper()
                value = (r.get("value") or "").strip()
                if ev == "meta":
                    k, _, v = value.partition("=")
                    fl.meta[k.strip()] = v.strip()
                    continue
                t = float(r["t"])
                if ev == "belt_off":
                    fl.belt_off = t
                elif ev == "door":
                    fl.exits.append(t)
                else:
                    if not who:
                        raise SystemExit(f"{path}:{n}: event {ev!r} needs a seat in 'who'")
                    row, col = parse_seat(who)
                    p = fl.people.setdefault(who, Person(who, row, col))
                    p.events.setdefault(ev, t)
                    if ev == "bin_open" and value:
                        p.bin_row = int(value) - 1
                    if ev == "exit":
                        fl.exits.append(t)
    for fl in flights.values():
        fl.exits.sort()
    return list(flights.values())


# ---------------------------------------------------------------------------
# From events to parameters
# ---------------------------------------------------------------------------

def quantile_table(xs: list[float]) -> tuple[tuple[float, float], ...]:
    """Empirical quantiles (linear interpolation between order statistics)."""
    s = sorted(xs)
    out = []
    for p in QUANTILE_PS:
        pos = p * (len(s) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(s) - 1)
        out.append((p, round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 2)))
    return tuple(out)


@dataclass
class Estimate:
    param: str
    value: object
    n: int
    how: str


def estimate(flights: list[Flight], min_n: int = 8) -> tuple[list[Estimate], list[str]]:
    """Returns the parameters with enough data, and notes on the ones without."""
    samples: dict[str, list[float]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    offsets: list[int] = []
    door_gaps: list[float] = []

    for fl in flights:
        for p in fl.people.values():
            e = p.events
            dist = COLUMN_AISLE_DISTANCE.get(p.col)
            if dist is None:
                continue
            if fl.belt_off is not None and "stand" in e and dist == 0:
                samples["prep_time"].append(e["stand"] - fl.belt_off)
            if "stand" in e and "edge" in e and dist == 0:
                samples["stand_time"].append(e["edge"] - e["stand"])
            if "edge" in e and "step_in" in e:
                pass                               # waiting at the edge: that is the policy, not a parameter
            if "step_in" in e:
                counts["stepped_in"] += 1
                nxt = [e[k] for k in ("bin_open", "walk", "back") if k in e and e[k] >= e["step_in"]]
                if nxt and min(nxt) - e["step_in"] <= 5.0:
                    samples["step_in_time"].append(min(nxt) - e["step_in"])
                if "let_in" in e:
                    counts["let_in"] += 1
                if "no_let_in" in e:
                    counts["no_let_in"] += 1
            if "bin_open" in e:
                counts["with_bag"] += 1
                if "bag_down" in e and e["bag_down"] > e["bin_open"]:
                    # skip bags started in the row and finished in the aisle
                    if not ("step_in" in e and e["bin_open"] < e["step_in"] < e["bag_down"]):
                        samples["bag_time"].append(e["bag_down"] - e["bin_open"])
                if p.bin_row is not None:
                    offsets.append(p.bin_row - p.row)
                    # would they take it down in the row?  Only observable for
                    # people who had to wait at the edge for a while
                    if (abs(p.bin_row - p.row) <= 1 and "step_in" in e and "edge" in e
                            and e["step_in"] - e["edge"] >= 3.0):
                        counts["reachable_bags"] += 1
                        if e["bin_open"] < e["step_in"]:
                            counts["prefetched"] += 1
        # passengers who stood up after their row-mates were already out
        for p in fl.people.values():
            dist = COLUMN_AISLE_DISTANCE.get(p.col, 0)
            if dist == 0 or "edge" not in p.events:
                continue
            inner = [fl.people.get(f"{p.row + 1}{c}") for c in
                     (["C", "B"] if p.col in "AB" else ["D", "E"])][:dist]
            outs = [q.events.get("step_in") for q in inner if q is not None]
            if len(outs) == dist and all(o is not None for o in outs):
                start = max(max(outs), p.events.get("stand", 0.0))
                samples["_to_edge_" + str(dist)].append(p.events["edge"] - start)
        gaps = [b - a for a, b in zip(fl.exits, fl.exits[1:])]
        door_gaps.extend(gaps)

    est: list[Estimate] = []
    notes: list[str] = []

    def add_quantiles(param: str, xs: list[float], how: str):
        if len(xs) >= min_n:
            est.append(Estimate(param, quantile_table(xs), len(xs), how))
        else:
            notes.append(f"{param}: {len(xs)} samples (need {min_n}) - {how}")

    add_quantiles("prep_time", samples["prep_time"], "aisle seats: stand - belt_off")
    add_quantiles("stand_time", samples["stand_time"], "aisle seats: edge - stand")
    add_quantiles("step_in_time", samples["step_in_time"], "step_in -> next action (bin/walk/back), <= 5 s")
    add_quantiles("bag_time", samples["bag_time"], "bag_down - bin_open")

    # lateral time per seat: time to reach the edge once the row is clear,
    # minus the median stand-up time
    stand_med = (statistics.median(samples["stand_time"]) if len(samples["stand_time"]) >= min_n
                 else Config().stand_time[len(Config().stand_time) // 2][1])
    lateral = []
    for dist in (1, 2):
        lateral += [max(0.2, (x - stand_med) / dist) for x in samples["_to_edge_" + str(dist)]]
    add_quantiles("lateral_time", lateral, "middle/window: (edge - max(stand, row-mates out) - stand) / seats")

    if counts["stepped_in"] >= min_n:
        est.append(Estimate("p_bag", round(counts["with_bag"] / counts["stepped_in"], 3),
                            counts["stepped_in"], "bin_open / step_in"))
    else:
        notes.append(f"p_bag: {counts['stepped_in']} passengers seen stepping in (need {min_n})")
    if counts["reachable_bags"] >= min_n:
        est.append(Estimate("p_prefetch", round(counts["prefetched"] / counts["reachable_bags"], 3),
                            counts["reachable_bags"],
                            "waited >= 3 s at the edge, bin within 1 row: opened before step_in"))
    else:
        notes.append(f"p_prefetch: {counts['reachable_bags']} bags within reach (need {min_n})")
    if len(offsets) >= min_n:
        table = []
        for o in range(-3, 4):
            share = sum(1 for x in offsets if max(-3, min(3, x)) == o) / len(offsets)
            table.append((o, round(share, 3)))
        est.append(Estimate("bin_offsets", tuple(table), len(offsets), "bin row - seat row (clipped to ±3)"))
    else:
        notes.append(f"bin_offsets: {len(offsets)} bins with a row (need {min_n})")
    decided = counts["let_in"] + counts["no_let_in"]
    if decided >= min_n:
        est.append(Estimate("p_yield", round(counts["let_in"] / decided, 3), decided,
                            "let_in / (let_in + no_let_in)"))
    else:
        notes.append(f"p_yield: {decided} step-ins marked let_in / no_let_in (need {min_n})")
    if len(door_gaps) >= min_n:
        s = sorted(door_gaps)
        est.append(Estimate("door_headway", round(s[int(0.1 * (len(s) - 1))], 2), len(s),
                            "10th percentile of time between exits"))
    else:
        notes.append(f"door_headway: {len(door_gaps)} gaps between exits (need {min_n})")
    return est, notes


def calibrated_config(est: list[Estimate], base: Config | None = None) -> Config:
    cfg = base or Config()
    for e in est:
        cfg = replace(cfg, **{e.param: e.value})
    return cfg


# ---------------------------------------------------------------------------
# Synthetic annotations: record a simulated flight the way an annotator would
# ---------------------------------------------------------------------------

def synthetic_annotations(sim: Simulator, video: str) -> list[dict]:
    """Run `sim` and log the events an annotator would mark on a video."""
    rows = [dict(video=video, t=0.0, who="", event="belt_off", value="", note="")]
    rows.append(dict(video=video, t=0, who="", event="meta", value=f"rows={sim.cfg.rows}", note=""))
    prev = {p.pid: p.state for p in sim.passengers}
    prev_bag = {p.pid: p.bag_left for p in sim.passengers}

    def log(p, event, value=""):
        rows.append(dict(video=video, t=round(sim.time, 3), who=f"{p.row + 1}{p.col}",
                         event=event, value=value, note=""))

    def coming(r: int) -> bool:
        # the nearest person behind row r is walking and less than 2 m away
        back_edge = sim.row_x(r) + sim.cfg.seat_pitch / 2
        for q in sim.aisle:
            if q.x > back_edge:
                return q.state == PaxState.WALKING and (q.x - q.length / 2) - back_edge < 2.0
        return False

    while sim.n_inside:
        held_before = {q.yield_row for q in sim.aisle if q.yield_row is not None}
        coming_before = {r for r in range(sim.cfg.rows) if coming(r)}
        sim.step()
        for p in sim.passengers:
            st, was = p.state, prev[p.pid]
            if st != was:
                if st == PaxState.TO_EDGE:
                    log(p, "stand")
                elif st == PaxState.AT_EDGE:
                    log(p, "edge")
                elif st == PaxState.ENTERING:
                    log(p, "step_in")
                    if p.row in held_before:
                        log(p, "let_in")
                    elif p.row in coming_before:
                        log(p, "no_let_in")
                elif st == PaxState.BACKTRACK:
                    log(p, "back")
                elif st == PaxState.RETRIEVING:
                    log(p, "bin_open", str(p.bin_row + 1))
                elif st == PaxState.WALKING and was in (PaxState.RETRIEVING, PaxState.ENTERING):
                    log(p, "walk")
                elif st == PaxState.EXITED:
                    log(p, "exit")
            if (st == PaxState.AT_EDGE and p.has_bag and prev_bag[p.pid] == p.bag
                    and p.bag_left < p.bag):
                log(p, "bin_open", str(p.bin_row + 1))          # started taking it down in the row
            if p.has_bag and prev_bag[p.pid] > 0 and p.bag_left == 0:
                log(p, "bag_down")                              # out of the bin, in hand
            prev[p.pid] = st
            prev_bag[p.pid] = p.bag_left
    return rows


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["video", "t", "who", "event", "value", "note"])
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Validation against the observed exits
# ---------------------------------------------------------------------------

def validate(flights: list[Flight], cfg: Config, k: int = 40) -> None:
    for fl in flights:
        if len(fl.exits) < 10 or fl.belt_off is None:
            print(f"  {fl.video}: not enough exits to validate")
            continue
        rows = int(fl.meta.get("rows", cfg.rows))
        occ = float(fl.meta.get("occupancy", cfg.p_occupancy))
        c = replace(cfg, rows=rows, p_occupancy=occ)
        res = run_experiment(c.p_yield, k=k, seed=1, config=c)
        totals = sorted(r["total"] for r in res)
        observed = fl.exits[-1] - fl.belt_off
        rank = sum(1 for t in totals if t < observed) / len(totals)
        print(f"  {fl.video}: observed {observed:.0f} s with {len(fl.exits)} exits; model "
              f"median {statistics.median(totals):.0f} s (P10 {totals[len(totals) // 10]:.0f}, "
              f"P90 {totals[len(totals) * 9 // 10]:.0f}); observed is at the {rank:.0%} percentile")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def fmt(value) -> str:
    if isinstance(value, tuple):
        return " ".join(f"{a:g}:{b:g}" for a, b in value)
    return f"{value:g}" if isinstance(value, float) else str(value)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="*", help="annotation CSV files")
    ap.add_argument("--out", help="write the calibrated parameters (JSON) here")
    ap.add_argument("--min-n", type=int, default=8, help="minimum samples to estimate a parameter")
    ap.add_argument("--validate", action="store_true", help="compare total times with the calibrated model")
    ap.add_argument("--synthetic", type=int, metavar="N",
                    help="ignore CSVs; annotate N simulated flights and calibrate from them")
    ap.add_argument("--synthetic-csv", help="with --synthetic, also save the annotations here")
    args = ap.parse_args()

    if args.synthetic:
        rows = []
        for i in range(args.synthetic):
            rows += synthetic_annotations(Simulator(Config(), seed=1000 + i), f"synthetic-{i + 1}")
        path = args.synthetic_csv
        if path is None:
            tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
            tmp.close()
            path = tmp.name
        write_csv(rows, path)
        args.csv = [path]
    if not args.csv:
        ap.error("give annotation CSVs, or --synthetic N")

    flights = read_flights(args.csv)
    n_people = sum(len(f.people) for f in flights)
    print(f"\n{len(flights)} flight(s), {n_people} passengers annotated\n")
    est, notes = estimate(flights, args.min_n)
    default = Config()
    print(f"{'parameter':15s} {'n':>5s}  {'estimate':42s} default")
    for e in est:
        print(f"{e.param:15s} {e.n:5d}  {fmt(e.value):42s} {fmt(getattr(default, e.param))}")
        print(f"{'':15s} {'':5s}  ({e.how})")
    if notes:
        print("\nNot enough data (defaults kept):")
        for n in notes:
            print(f"  {n}")
    cfg = calibrated_config(est)
    if args.out:
        overrides = {e.param: config_to_dict(cfg)[e.param] for e in est}
        with open(args.out, "w") as f:
            json.dump(overrides, f, indent=2)
        print(f"\nCalibrated parameters written to {args.out}  "
              f"(use: python simulate_deboarding.py --config {args.out})")
    if args.validate:
        print("\nValidation (total deboarding time):")
        validate(flights, cfg)
    print()


if __name__ == "__main__":
    sys.exit(main())
