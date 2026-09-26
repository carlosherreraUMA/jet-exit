#!/usr/bin/env python3
"""
Refresh the data embedded in web/index.html from the Python simulator.

  PARITY       reference runs the page re-simulates to prove its JS port is
               bit-identical (total ticks + a checksum of every exit tick).
  SENSITIVITY  the one-at-a-time sensitivity rows from a `sweep --csv` run.

Usage:
  python simulate_deboarding.py sweep --k 200 --seed 1 --csv sweep.csv
  python export_parity.py --sweep-csv sweep.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from simulate_deboarding import Simulator

PAGE = Path(__file__).parent / "web" / "index.html"

CASES = [
    dict(seed=1, p_yield=1.0),
    dict(seed=1, p_yield=0.0),
    dict(seed=2, p_yield=0.5),
    dict(seed=3, p_yield=0.25, rows=12, cells_per_row=1.0, door_headway=2.0, p_prefetch=0.0),
    dict(seed=4, p_yield=0.75, rows=20, p_occupancy=0.7, p_bag=0.9, cells_per_row=2.0),
    dict(seed=5, p_yield=1.0, rows=8, dt=1.0, gap_cells=3, front_cells=1),
    dict(seed=4294967295, p_yield=0.1, rows=15, prep_range=[0, 20, 8], bag_range=[1, 30, 4]),
]


def parity_cases() -> list[dict]:
    out = []
    for kw in CASES:
        sim = Simulator(**kw)
        sim.run()
        checksum = 0
        for p in sim.passengers:
            checksum = (checksum + (p.pid + 1) * p.t_exit) % 2147483647
        out.append({"kwargs": kw, "ticks": sim.tick, "checksum": checksum, "n": len(sim.passengers)})
    return out


def sensitivity_rows(path: str) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["sweep"] == "p_yield":
                continue
            courtesy = float(r["mean_total"])
            diff = float(r["diff"])
            rows.append({
                "param": r["sweep"],
                "value": float(r["value"]),
                "courtesy": round(courtesy, 2),
                "aisle": round(courtesy - diff, 2),
                "diff": round(diff, 2),
                "lo": round(float(r["ci_lo"]), 2),
                "hi": round(float(r["ci_hi"]), 2),
            })
    return rows


def replace_block(html: str, name: str, value) -> str:
    pattern = re.compile(rf"/\*{name}\*/.*?/\*END_{name}\*/", re.S)
    if not pattern.search(html):
        raise SystemExit(f"marker /*{name}*/ not found in {PAGE}")
    return pattern.sub(lambda _: f"/*{name}*/{json.dumps(value)}/*END_{name}*/", html)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sweep-csv", help="CSV written by `simulate_deboarding.py sweep --csv`")
    args = parser.parse_args()

    html = PAGE.read_text()
    html = replace_block(html, "PARITY", parity_cases())
    if args.sweep_csv:
        html = replace_block(html, "SENSITIVITY", sensitivity_rows(args.sweep_csv))
    PAGE.write_text(html)
    print(f"Updated {PAGE}")


if __name__ == "__main__":
    main()
