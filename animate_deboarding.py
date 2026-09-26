#!/usr/bin/env python3
"""
=============================================================================
  ASCII Animation for Airplane Deboarding Simulation
=============================================================================

Animates courtesy (p_yield=1) and aisle_priority (p_yield=0) side by side on
the same plane (same seed, same passengers).  Front of the plane and the door
are at the top; every aisle cell is drawn.

Usage:
  python animate_deboarding.py                          # both, 10 rows
  python animate_deboarding.py --policy courtesy        # just one
  python animate_deboarding.py --rows 15 --seed 42 --fps 6
  python animate_deboarding.py --p_prefetch 0 --no-color

Controls:
  Ctrl+C to quit at any time.
=============================================================================
"""

from __future__ import annotations

import argparse
import sys
import time

from simulate_deboarding import (
    POLICIES, PaxState, Simulator, add_model_args, sim_kwargs_from_args,
)

# ---------------------------------------------------------------------------
# ANSI colours (gracefully disabled with --no-color)
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

STATE_COLOR = {
    PaxState.SEATED: "\033[33m",      # yellow  -- preparing in seat
    PaxState.WAITING: "\033[92m",     # green   -- ready, wants to enter
    PaxState.ENTERING: "\033[96m",    # cyan    -- stepping into the aisle
    PaxState.RETRIEVING: "\033[95m",  # magenta -- taking bag, blocking
    PaxState.WALKING: "\033[94m",     # blue    -- walking to the door
    PaxState.EXITED: "\033[90m",      # grey
}
SEAT_GLYPH = {
    PaxState.SEATED: "▪", PaxState.WAITING: "●",
}
AISLE_GLYPH = {
    PaxState.ENTERING: "E", PaxState.RETRIEVING: "B", PaxState.WALKING: "▲",
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


def _cell(sim: Simulator, i: int) -> str:
    pax = sim.aisle[i]
    if pax is None:
        return c(DIM, "  ·  ")
    label = f"{AISLE_GLYPH[pax.state]}{pax.row + 1:2d}{pax.col}"
    return c(STATE_COLOR[pax.state], f" {label}")


def render_frame(sim: Simulator, title: str) -> list[str]:
    """Fixed-width lines (29 visible chars) for one simulator."""
    n_out = len(sim.passengers) - sim.n_inside
    n_total = max(len(sim.passengers), 1)
    bar_len = 20
    done = int(n_out / n_total * bar_len)
    lines = [
        c(BOLD, f"{title:<29s}"),
        f"t = {sim.time:6.1f} s   out {n_out:3d}/{len(sim.passengers)}".ljust(29),
        c(STATE_COLOR[PaxState.WALKING], "█" * done) + c(DIM, "░" * (bar_len - done)) + " " * 9,
        c(DIM, "row  A B C  aisle  D E F     "),
        c(DIM, "              ▲ door         "),
    ]
    for i in range(sim.front_cells):
        lines.append(f"            {_cell(sim, i)}            ")
    for r in range(sim.rows):
        for j, cell in enumerate(sim.row_cells[r]):
            if j == 0:
                left = " ".join(_seat(sim, r, col) for col in "ABC")
                right = " ".join(_seat(sim, r, col) for col in "DEF")
                lines.append(f"{c(DIM, f'{r + 1:3d}')}  {left}  {_cell(sim, cell)}  {right}     ")
            else:
                lines.append(f"            {_cell(sim, cell)}            ")
    return lines


def legend() -> str:
    return (
        f"  {c(STATE_COLOR[PaxState.SEATED], '▪ preparing')}  "
        f"{c(STATE_COLOR[PaxState.WAITING], '● wants to enter')}  "
        f"{c(STATE_COLOR[PaxState.ENTERING], 'E entering')}  "
        f"{c(STATE_COLOR[PaxState.RETRIEVING], 'B bag (blocks)')}  "
        f"{c(STATE_COLOR[PaxState.WALKING], '▲ walking')}"
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

def animate(policies: list[str], seed: int, fps: float, sim_kwargs: dict) -> dict[str, float]:
    """Step all simulators together and draw them side by side."""
    sims = {pol: Simulator(p_yield=POLICIES[pol], seed=seed, **sim_kwargs) for pol in policies}
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
            frames = [render_frame(sim, f"{pol} (p_yield={sim.p_yield:g})")
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
    parser.add_argument("--fps", type=float, default=8,
                        help="Frames per second; each frame is one tick (default: 8)")
    parser.add_argument("--policy", default="both", choices=[*POLICIES, "both"],
                        help="Which policy to animate (default: both, side by side)")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color codes")
    add_model_args(parser)
    parser.set_defaults(rows=10)
    args = parser.parse_args()

    global USE_COLOR
    USE_COLOR = not args.no_color

    policies = list(POLICIES) if args.policy == "both" else [args.policy]
    finished = animate(policies, args.seed, args.fps, sim_kwargs_from_args(args))

    if len(finished) == 2:
        ts, ta = finished["courtesy"], finished["aisle_priority"]
        print(f"\n  courtesy − aisle_priority: {ts - ta:+.1f} s "
              f"({(ts - ta) / ta * 100:+.1f}%)  -- one plane, one seed; "
              f"use simulate_deboarding.py for statistics.\n")


if __name__ == "__main__":
    main()
