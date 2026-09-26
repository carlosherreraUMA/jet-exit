#!/usr/bin/env python3
"""
=============================================================================
  ASCII Animation for Airplane Deboarding Simulation
=============================================================================

Animates courtesy (p_yield=1) and aisle_priority (p_yield=0) side by side on
the same plane (same seed, same passengers).  Front of the plane and the door
are at the top.  The aisle is continuous; each line shows who is standing in
that row's stretch of aisle (up to three people).

Usage:
  python animate_deboarding.py                          # both, 10 rows
  python animate_deboarding.py --policy courtesy        # just one
  python animate_deboarding.py --rows 15 --seed 42 --fps 30
  python animate_deboarding.py --set p_prefetch=0 --no-color

Controls:
  Ctrl+C to quit at any time.
=============================================================================
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace

from simulate_deboarding import (
    POLICIES, PaxState, Simulator, add_model_args, config_from_args,
)

# ---------------------------------------------------------------------------
# ANSI colours (gracefully disabled with --no-color)
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

STATE_COLOR = {
    PaxState.SEATED: "\033[33m",      # yellow  -- preparing in seat
    PaxState.READY: "\033[32m",       # green   -- ready, waiting for the row
    PaxState.TO_EDGE: "\033[92m",     # bright green -- getting out of the row
    PaxState.AT_EDGE: "\033[92m",
    PaxState.ENTERING: "\033[96m",    # cyan    -- stepping into the aisle
    PaxState.BACKTRACK: "\033[95m",   # magenta -- walking back to a bin
    PaxState.RETRIEVING: "\033[95m",  # magenta -- taking bag, blocking
    PaxState.WALKING: "\033[94m",     # blue    -- walking to the door
}
SEAT_GLYPH = {
    PaxState.SEATED: "▪", PaxState.READY: "○", PaxState.TO_EDGE: "●", PaxState.AT_EDGE: "●",
}
AISLE_GLYPH = {
    PaxState.ENTERING: "E", PaxState.BACKTRACK: "▼", PaxState.RETRIEVING: "B",
    PaxState.WALKING: "▲",
}

USE_COLOR = True


def c(code: str, text: str) -> str:
    return f"{code}{text}{RESET}" if USE_COLOR else text


# ---------------------------------------------------------------------------
# Rendering (reads the live simulator; one frame per tick)
# ---------------------------------------------------------------------------

def _seat(sim: Simulator, row: int, col: str) -> str:
    pax = sim._pax_by_row_col.get((row, col))
    if pax is None:
        return c(DIM, "░")
    if pax.state in SEAT_GLYPH:
        return c(STATE_COLOR[pax.state], SEAT_GLYPH[pax.state])
    return c(DIM, "·")


def _aisle_cell(people: list) -> str:
    """Up to three people standing in one row's stretch of aisle, 5 wide."""
    glyphs = []
    for p in people[:3]:
        g = AISLE_GLYPH[p.state]
        if p.state == PaxState.WALKING and p.yield_row is not None:
            g = "‖"                      # stopped to let a row out
        glyphs.append(c(STATE_COLOR[p.state], g))
    pad = 3 - len(glyphs)
    return " " + "".join(glyphs) + c(DIM, "·" * pad) + " "


def render_frame(sim: Simulator, title: str) -> list[str]:
    """Fixed-width lines (29 visible chars) for one simulator."""
    cfg = sim.cfg
    n_out = len(sim.passengers) - sim.n_inside
    n_total = max(len(sim.passengers), 1)
    bar_len = 20
    done = int(n_out / n_total * bar_len)

    # who stands in which row's stretch (front area = row -1)
    by_row: dict[int, list] = {}
    for p in sim.aisle:
        r = int((p.x - cfg.door_to_row1 + cfg.seat_pitch / 2) // cfg.seat_pitch)
        by_row.setdefault(max(-1, min(cfg.rows - 1, r)), []).append(p)

    lines = [
        c(BOLD, f"{title:<29s}"),
        f"t = {sim.time:6.1f} s   out {n_out:3d}/{len(sim.passengers)}".ljust(29),
        c(STATE_COLOR[PaxState.WALKING], "█" * done) + c(DIM, "░" * (bar_len - done)) + " " * 9,
        c(DIM, "row  A B C  aisle  D E F     "),
        c(DIM, "              ▲ door         "),
        f"           {_aisle_cell(by_row.get(-1, []))}             ",
    ]
    for r in range(cfg.rows):
        left = " ".join(_seat(sim, r, col) for col in "ABC")
        right = " ".join(_seat(sim, r, col) for col in "DEF")
        lines.append(f"{c(DIM, f'{r + 1:3d}')}  {left}  {_aisle_cell(by_row.get(r, []))}  {right}     ")
    return lines


def legend() -> str:
    return (
        f"  {c(STATE_COLOR[PaxState.SEATED], '▪ preparing')}  "
        f"{c(STATE_COLOR[PaxState.READY], '○ ready')}  "
        f"{c(STATE_COLOR[PaxState.AT_EDGE], '● getting out')}  "
        f"{c(STATE_COLOR[PaxState.ENTERING], 'E entering')}  "
        f"{c(STATE_COLOR[PaxState.RETRIEVING], 'B bag  ▼ back to bin')}  "
        f"{c(STATE_COLOR[PaxState.WALKING], '▲ walking  ‖ letting a row out')}"
    )


def clear_and_print(lines: list[str], prev_line_count: int) -> int:
    """Move cursor up `prev_line_count` lines, then print new frame."""
    if prev_line_count > 0:
        sys.stdout.write(f"\033[{prev_line_count}A\033[J")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
    return len(lines)


# ---------------------------------------------------------------------------
# Animation loop
# ---------------------------------------------------------------------------

def animate(policies: list[str], seed: int, fps: float, cfg) -> dict[str, float]:
    """Step all simulators together and draw them side by side."""
    sims = {pol: Simulator(replace(cfg, p_yield=POLICIES[pol]), seed=seed) for pol in policies}
    finished: dict[str, float] = {}
    prev = 0
    delay = 1.0 / fps
    try:
        while True:
            for pol, sim in sims.items():
                if sim.n_inside:
                    sim.step()
                elif pol not in finished:
                    finished[pol] = sim.time
            frames = [render_frame(sim, f"{pol} (p_yield={sim.cfg.p_yield:g})")
                      for pol, sim in sims.items()]
            lines = ["    ".join(parts) for parts in zip(*frames)]
            lines += ["", legend()]
            for pol in policies:
                if pol in finished:
                    lines.append(c(BOLD, f"  {pol}: done in {finished[pol]:.1f} s"))
            prev = clear_and_print(lines, prev)
            if len(finished) == len(sims):
                break
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\n  [Interrupted]")
    return finished


def main():
    parser = argparse.ArgumentParser(description="ASCII animation of airplane deboarding.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fps", type=float, default=16,
                        help="Frames per second; each frame is one tick (default: 16)")
    parser.add_argument("--policy", default="both", choices=[*POLICIES, "both"],
                        help="Which policy to animate (default: both, side by side)")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color codes")
    add_model_args(parser)
    parser.set_defaults(rows=10)
    args = parser.parse_args()

    global USE_COLOR
    USE_COLOR = not args.no_color

    policies = list(POLICIES) if args.policy == "both" else [args.policy]
    finished = animate(policies, args.seed, args.fps, config_from_args(args))

    if len(finished) == 2:
        ts, ta = finished["courtesy"], finished["aisle_priority"]
        print(f"\n  courtesy − aisle_priority: {ts - ta:+.1f} s "
              f"({(ts - ta) / ta * 100:+.1f}%)  -- one plane, one seed; "
              f"use simulate_deboarding.py for statistics.\n")


if __name__ == "__main__":
    main()
