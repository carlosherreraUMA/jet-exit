// ===========================================================================
// Deboarding model: exact port of simulate_deboarding.py.
// Keep both in sync; `python export_parity.py` refreshes web/data.js with the
// Python defaults and reference runs this port must reproduce bit for bit.
// Only + - * / Math.sqrt Math.floor Math.abs Math.min Math.max are used, so
// results are identical to CPython's.
// ===========================================================================
(function (root) {
  "use strict";

  class Rng {
    constructor(seed) { this.a = seed >>> 0; }
    nextU32() {
      this.a = (this.a + 0x6D2B79F5) >>> 0;
      const a = this.a;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return (t ^ (t >>> 14)) >>> 0;
    }
    random() { return this.nextU32() / 4294967296; }
  }

  function sampleQuantiles(knots, u) {
    for (let i = 1; i < knots.length; i++) {
      const [p1, v1] = knots[i];
      if (u < p1 || i === knots.length - 1) {
        const [p0, v0] = knots[i - 1];
        if (p1 === p0) return v1;
        return v0 + (u - p0) / (p1 - p0) * (v1 - v0);
      }
    }
    return knots[knots.length - 1][1];
  }

  function sampleDiscrete(table, u) {
    let acc = 0.0;
    for (const [value, prob] of table) {
      acc += prob;
      if (u < acc) return value;
    }
    return table[table.length - 1][0];
  }

  const toTicks = (x) => Math.floor(x + 0.5);

  const S = {
    SEATED: 0, READY: 1, TO_EDGE: 2, AT_EDGE: 3, ENTERING: 4,
    BACKTRACK: 5, RETRIEVING: 6, WALKING: 7, EXITED: 8,
  };
  const COLS = "ABCDEF";
  const DIST = { C: 0, D: 0, B: 1, E: 1, A: 2, F: 2 };
  const BLOCKING = { A: ["B", "C"], B: ["C"], C: [], D: [], E: ["D"], F: ["D", "E"] };
  const LEFT = new Set(["A", "B", "C"]);
  const outOfRow = (st) => st === S.ENTERING || st === S.BACKTRACK || st === S.RETRIEVING
    || st === S.WALKING || st === S.EXITED;

  // first index whose x >= v (left) or > v (right); `aisle` is sorted by x
  function bisectLeft(aisle, v) {
    let lo = 0, hi = aisle.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (aisle[mid].x < v) lo = mid + 1; else hi = mid; }
    return lo;
  }
  function bisectRight(aisle, v) {
    let lo = 0, hi = aisle.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (v < aisle[mid].x) hi = mid; else lo = mid + 1; }
    return lo;
  }

  class Sim {
    // config: full parameter object (see DEFAULT_CONFIG in data.js)
    constructor(config, seed = 0) {
      const cfg = this.cfg = config;
      this.dt = cfg.dt;
      this.gen = new Rng(seed);
      this.dyn = new Rng((seed ^ 0x9E3779B9) >>> 0);
      this.tick = 0;
      this.lastExit = -Infinity;
      this.doorTicks = Math.max(1, this.ticks(cfg.door_headway));
      this.near = cfg.body_depth[cfg.body_depth.length - 1][1] + cfg.bag_length + cfg.s0;
      this.aisle = [];
      this.reservations = [];
      this.heldRows = new Set();
      this.fa = new Float64Array(256);   // scratch buffers for freeSpot
      this.fb = new Float64Array(256);
      this.pax = [];
      this.byRC = new Map();
      this.seatsByRow = Array.from({ length: cfg.rows }, () => []);
      this.generate();
      for (const p of this.pax) {
        p.blockers = BLOCKING[p.col].map((c) => this.byRC.get(p.row * 8 + COLS.indexOf(c))).filter(Boolean);
      }
      this.nInside = this.pax.length;
    }

    ticks(seconds) { return toTicks(seconds / this.dt); }
    rowX(r) { return this.cfg.door_to_row1 + r * this.cfg.seat_pitch; }
    binX(p) { return this.rowX(p.binRow); }
    needsBacktrack(p) { return p.hasBag && this.binX(p) - this.rowX(p.row) > this.cfg.reach; }
    bagPending(p) { return p.hasBag && !p.carrying; }

    generate() {
      const cfg = this.cfg, rng = this.gen;
      for (let row = 0; row < cfg.rows; row++) {
        for (const col of COLS) {
          if (rng.random() >= cfg.p_occupancy) continue;
          const hasBag = rng.random() < cfg.p_bag;
          const prep = sampleQuantiles(cfg.prep_time, rng.random());
          const stand = sampleQuantiles(cfg.stand_time, rng.random());
          const lateral = sampleQuantiles(cfg.lateral_time, rng.random());
          const stepIn = sampleQuantiles(cfg.step_in_time, rng.random());
          const v0 = sampleQuantiles(cfg.walk_speed, rng.random());
          const depth = sampleQuantiles(cfg.body_depth, rng.random());
          let bag = 0.0, offset = 0, prefetch = false;
          if (hasBag) {
            bag = sampleQuantiles(cfg.bag_time, rng.random());
            offset = sampleDiscrete(cfg.bin_offsets, rng.random());
            prefetch = rng.random() < cfg.p_prefetch;
          }
          this.addPassenger(row, col, hasBag, prep, stand, lateral, stepIn, v0, depth, bag, offset, prefetch);
        }
      }
    }

    addPassenger(row, col, hasBag, prep, stand, lateral, stepIn, v0, depth, bag, binOffset, prefetch) {
      const dist = DIST[col];
      const binRow = hasBag ? Math.min(Math.max(row + binOffset, 0), this.cfg.rows - 1) : row;
      const p = {
        pid: this.pax.length, row, col, hasBag, binRow, prefetch: prefetch && hasBag,
        left: LEFT.has(col), dist,
        prep: this.ticks(prep),
        toEdge: Math.max(1, this.ticks(stand + lateral * dist)),
        stepIn: Math.max(1, this.ticks(stepIn)),
        bag: hasBag ? Math.max(1, this.ticks(bag)) : 0,
        v0, depth,
        state: S.SEATED, prepLeft: 0, edgeLeft: 0, enterLeft: 0, bagLeft: 0,
        carrying: false, x: -1.0, v: 0.0, length: depth,
        yieldRow: null, yields: new Map(), blockers: null,
        tReady: -1, tEdge: -1, tEnter: -1, tExit: -1,
      };
      p.prepLeft = p.prep;
      p.bagLeft = p.bag;
      this.pax.push(p);
      this.byRC.set(row * 8 + COLS.indexOf(col), p);
      this.seatsByRow[row].push(p);
      return p;
    }

    leavingForward(p) {
      if (p.state === S.TO_EDGE || p.state === S.AT_EDGE) {
        return !(this.bagPending(p) && this.needsBacktrack(p));
      }
      return p.state === S.ENTERING;
    }

    rowWantsOut(r) {
      for (const p of this.seatsByRow[r]) {
        if (p.state === S.TO_EDGE || p.state === S.AT_EDGE) {
          if (this.leavingForward(p)) return true;
        } else if (p.state === S.READY) {
          for (const b of p.blockers) if (this.leavingForward(b)) return true;
        }
      }
      return false;
    }

    phaseTimers() {
      const cfg = this.cfg;
      for (const p of this.pax) {
        let st = p.state;
        if (st === S.SEATED) {
          if (p.prepLeft > 0) p.prepLeft--;
          if (p.prepLeft === 0) { p.state = st = S.READY; p.tReady = this.tick; }
        }
        if (st === S.READY) {
          let clear = true;
          for (const b of p.blockers) if (!outOfRow(b.state)) { clear = false; break; }
          if (clear) { p.state = S.TO_EDGE; p.edgeLeft = p.toEdge; }
        } else if (st === S.TO_EDGE) {
          p.edgeLeft--;
          if (p.edgeLeft <= 0) { p.state = S.AT_EDGE; p.tEdge = this.tick; }
        } else if (st === S.AT_EDGE) {
          if (p.prefetch && p.bagLeft > 0 && !this.needsBacktrack(p)
            && Math.abs(this.binX(p) - this.rowX(p.row)) <= cfg.reach) {
            p.bagLeft--;
            if (p.bagLeft === 0) { p.carrying = true; p.length = p.depth + cfg.bag_length; }
          }
        } else if (st === S.ENTERING) {
          p.enterLeft--;
          if (p.enterLeft <= 0) this.afterEntering(p);
        } else if (st === S.RETRIEVING) {
          if (p.bagLeft > 0) p.bagLeft--;
          if (p.bagLeft === 0) this.tryFinishBag(p);
        }
      }
    }

    afterEntering(p) {
      p.v = 0.0;
      if (!this.bagPending(p)) p.state = S.WALKING;
      else if (this.needsBacktrack(p)) p.state = S.BACKTRACK;
      else if (this.binX(p) < p.x - this.cfg.reach) p.state = S.WALKING;
      else p.state = S.RETRIEVING;
    }

    tryFinishBag(p) {
      const cfg = this.cfg;
      const k = this.aisle.indexOf(p);
      const front = p.x - p.length / 2;
      let room = Infinity;
      if (k > 0) {
        const lead = this.aisle[k - 1];
        room = front - (lead.x + lead.length / 2);
      }
      for (const res of this.reservations) {
        if (res.owner !== p && res.b <= front) room = Math.min(room, front - res.b);
      }
      if (room < cfg.bag_length + cfg.s0) return;
      p.x = p.x - cfg.bag_length / 2;
      p.length = p.length + cfg.bag_length;
      p.carrying = true;
      p.state = S.WALKING;
      p.v = 0.0;
      this.reservations = this.reservations.filter((r) => r.owner !== p);
    }

    phaseDoor() {
      if (!this.aisle.length) return;
      const p = this.aisle[0];
      if (p.state === S.WALKING && p.x <= 0.0 && this.tick - this.lastExit >= this.doorTicks) {
        p.state = S.EXITED; p.tExit = this.tick; p.v = 0.0;
        this.aisle.shift();
        this.lastExit = this.tick;
        this.nInside--;
      }
    }

    phaseMove() {
      const cfg = this.cfg, dt = this.dt;
      const oldX = this.aisle.map((p) => p.x);
      const wants = new Map();
      const rowWants = (r) => {
        if (!wants.has(r)) wants.set(r, this.rowWantsOut(r));
        return wants.get(r);
      };
      this.heldRows.clear();
      for (let k = 0; k < this.aisle.length; k++) {
        const p = this.aisle[k];
        if (p.state === S.BACKTRACK) { this.moveBack(k, p, oldX); continue; }
        if (p.state !== S.WALKING) { p.v = 0.0; continue; }

        const front = p.x - p.length / 2;
        let gap = Infinity;
        if (k > 0) {
          const lead = this.aisle[k - 1];
          gap = front - (oldX[k - 1] + lead.length / 2);
        }
        for (const res of this.reservations) {
          if (res.b <= front) gap = Math.min(gap, front - res.b);
        }

        if (p.yieldRow !== null && !rowWants(p.yieldRow)) p.yieldRow = null;
        if (p.yieldRow === null) {
          const r = Math.floor((front - cfg.door_to_row1 - cfg.seat_pitch / 2) / cfg.seat_pitch);
          if (0 <= r && r < cfg.rows) {
            const backEdge = cfg.door_to_row1 + r * cfg.seat_pitch + cfg.seat_pitch / 2;
            if (front - backEdge <= cfg.yield_distance && rowWants(r)) {
              let d = p.yields.get(r);
              if (d === undefined) { d = this.dyn.random() < cfg.p_yield; p.yields.set(r, d); }
              if (d) p.yieldRow = r;
            }
          }
        }
        if (p.yieldRow !== null) {
          this.heldRows.add(p.yieldRow);
          const backEdge = cfg.door_to_row1 + p.yieldRow * cfg.seat_pitch + cfg.seat_pitch / 2;
          gap = Math.min(gap, front - backEdge);
        }

        const v0 = p.carrying ? p.v0 * cfg.bag_speed_factor : p.v0;
        let vNew;
        if (p.v === 0.0 && gap - cfg.s0 < cfg.start_gap) vNew = 0.0;
        else {
          vNew = Math.min(v0, p.v + cfg.accel * dt, (gap - cfg.s0) / cfg.headway);
          if (vNew < 0.0) vNew = 0.0;
        }
        let travel = vNew * dt;

        let target = 0.0, atBin = false;
        if (this.bagPending(p)) { target = this.binX(p) + cfg.reach / 2; atBin = true; }
        const limit = p.x - target;
        if (travel >= limit) {
          travel = limit > 0.0 ? limit : 0.0;
          p.x = limit > 0.0 ? target : p.x;
          if (atBin) p.state = S.RETRIEVING;
        } else {
          p.x = p.x - travel;
        }
        p.v = travel / dt;
      }
    }

    moveBack(k, p, oldX) {
      const cfg = this.cfg;
      const target = this.binX(p) - cfg.reach / 2;
      let step = cfg.back_speed * this.dt;
      if (k + 1 < this.aisle.length) {
        const f = this.aisle[k + 1];
        const room = (oldX[k + 1] - f.length / 2) - (p.x + p.length / 2) - cfg.s0;
        if (room < step) step = room;
      }
      if (step < 0.0) step = 0.0;
      if (step >= target - p.x) { p.x = target; p.state = S.RETRIEVING; }
      else p.x = p.x + step;
      p.v = 0.0;
    }

    freeSpot(r, length) {
      const cfg = this.cfg;
      const xr = this.rowX(r);
      const lo = xr - cfg.seat_pitch / 2;
      const hi = xr + cfg.seat_pitch / 2;
      const fa = this.fa, fb = this.fb;
      let n = 0;
      let k = bisectLeft(this.aisle, lo - this.near);
      while (k < this.aisle.length && this.aisle[k].x < hi + this.near) {
        const q = this.aisle[k++];
        const half = (q.length + length) / 2 + cfg.s0;
        const a = q.x - half, b = q.x + half;
        if (b > lo && a < hi) { fa[n] = a; fb[n] = b; n++; }
      }
      for (const res of this.reservations) {
        const a = res.a - length / 2 - cfg.s0, b = res.b + length / 2 + cfg.s0;
        if (b > lo && a < hi) { fa[n] = a; fb[n] = b; n++; }
      }
      const isFree = (c) => {
        if (c < lo || c > hi) return false;
        for (let i = 0; i < n; i++) if (fa[i] < c && c < fb[i]) return false;
        return true;
      };
      // the row centre is the best possible spot; otherwise the closest free
      // edge of a forbidden interval (ties towards the door)
      if (isFree(xr)) return xr;
      let best = null;
      for (let i = 0; i < 2 * n; i++) {
        const c = i & 1 ? fb[i >> 1] : fa[i >> 1];
        if (!isFree(c)) continue;
        if (best === null || Math.abs(c - xr) < Math.abs(best - xr)
          || (Math.abs(c - xr) === Math.abs(best - xr) && c < best)) best = c;
      }
      return best;
    }

    gapOk(backEdge, r) {
      const cfg = this.cfg;
      const k = bisectRight(this.aisle, backEdge);
      if (k === this.aisle.length) return true;
      const q = this.aisle[k];
      if (q.state !== S.WALKING || q.yieldRow === r) return true;
      const dist = (q.x - q.length / 2) - backEdge;
      return dist >= cfg.gap_distance + q.v * cfg.gap_time;
    }

    segmentFree(lo, hi) {
      const s0 = this.cfg.s0;
      for (const q of this.aisle) {
        if (q.x + q.length / 2 + s0 > lo && q.x - q.length / 2 - s0 < hi) return false;
      }
      for (const res of this.reservations) if (res.b > lo && res.a < hi) return false;
      return true;
    }

    tryEnter(p, r) {
      const cfg = this.cfg;
      let spot;
      if (this.bagPending(p) && this.needsBacktrack(p)) {
        const b = (this.binX(p) - cfg.reach / 2) + p.length / 2 + cfg.s0;
        if (!this.gapOk(b, r)) return false;
        spot = this.freeSpot(r, p.length);
        if (spot === null || !this.segmentFree(spot - p.length / 2, b)) return false;
        this.reservations.push({ owner: p, a: spot - p.length / 2, b });
      } else {
        const offered = this.heldRows.has(r);
        if (!offered && !this.gapOk(this.rowX(r) + cfg.seat_pitch / 2, r)) return false;
        spot = this.freeSpot(r, p.length);
        if (spot === null) return false;
      }
      p.state = S.ENTERING;
      p.enterLeft = p.stepIn;
      p.tEnter = this.tick;
      p.x = spot;
      p.v = 0.0;
      this.aisle.splice(bisectLeft(this.aisle, spot), 0, p);
      return true;
    }

    phaseEnter() {
      for (let r = 0; r < this.cfg.rows; r++) {
        // at most one person per half-row is at the edge
        let first = null, second = null;
        for (const p of this.seatsByRow[r]) {
          if (p.state !== S.AT_EDGE) continue;
          if (first === null) first = p; else second = p;
        }
        if (first === null) continue;
        if (second !== null) {
          const leftFirst = (r + this.tick) % 2 === 0;
          const ka = first.left !== leftFirst ? 1 : 0, kb = second.left !== leftFirst ? 1 : 0;
          if (second.tEdge < first.tEdge || (second.tEdge === first.tEdge && kb < ka)) {
            const t = first; first = second; second = t;
          }
        }
        this.tryEnter(first, r);
        if (second !== null) this.tryEnter(second, r);
      }
    }

    step() {
      this.tick++;
      this.phaseTimers();
      this.phaseDoor();
      this.phaseMove();
      this.phaseEnter();
      return this.nInside > 0;
    }

    get time() { return this.tick * this.dt; }

    run(maxTicks = 200000) {
      while (this.nInside > 0 && this.tick < maxTicks) this.step();
      if (this.nInside > 0) throw new Error("simulation did not finish");
      return this.time;
    }

    metrics() {
      const ex = this.pax.map((p) => p.tExit * this.dt).sort((a, b) => a - b);
      if (!ex.length) return { total: 0, mean_exit: 0, p90_exit: 0 };
      let s = 0;
      for (const x of ex) s += x;
      return {
        total: this.time,
        mean_exit: s / ex.length,
        p90_exit: ex[Math.min(ex.length - 1, Math.floor(ex.length * 0.9))],
      };
    }
  }

  function runSeeds(k, seed) {
    const base = new Rng(seed);
    return Array.from({ length: k }, () => base.nextU32());
  }

  function pairedCI(a, b, seed, nBoot = 2000, alpha = 0.05) {
    const d = a.map((x, i) => x - b[i]);
    const n = d.length;
    let obs = 0;
    for (const x of d) obs += x;
    obs /= n;
    if (n < 2) return [obs, obs, obs];
    const rng = new Rng(seed);
    const boots = new Array(nBoot);
    for (let i = 0; i < nBoot; i++) {
      let s = 0;
      for (let j = 0; j < n; j++) s += d[Math.floor(rng.random() * n)];
      boots[i] = s / n;
    }
    boots.sort((x, y) => x - y);
    return [obs, boots[Math.floor(nBoot * alpha / 2)], boots[Math.floor(nBoot * (1 - alpha / 2)) - 1]];
  }

  const api = { Rng, Sim, S, COLS, DIST, sampleQuantiles, sampleDiscrete, runSeeds, pairedCI };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Deboarding = api;
})(this);
