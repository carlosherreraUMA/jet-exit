#!/usr/bin/env node
// Same sweep as `simulate_deboarding.py sweep`, on the bit-identical JS port
// (web/model.js), ~20x faster.  Writes the same CSV columns, so its output
// feeds `export_parity.py --sweep-csv` directly.
//
// Usage: node sweep.js [--k 100] [--seed 1] [--threads 4] [--csv sweep.csv]
const { Worker, isMainThread, parentPort, workerData } = require("worker_threads");
const path = require("path");
const fs = require("fs");
const { Sim, runSeeds, pairedCI } = require("./web/model.js");

if (!isMainThread) {
  for (const [cfg, seed] of workerData.jobs) {
    const sim = new Sim(cfg, seed);
    sim.run();
    parentPort.postMessage(sim.metrics().total);
  }
  return;
}

const { DEFAULT_CONFIG } = require("./web/data.js");
const arg = (name, def) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : def;
};
const K = Number(arg("k", 100));
const SEED = Number(arg("seed", 1));
const THREADS = Number(arg("threads", 4));
const CSV = arg("csv", null);

// Keep in sync with SWEEPS and _with_override in simulate_deboarding.py.
const SWEEPS = {
  p_prefetch: [0.0, 0.5, 0.8, 1.0],
  p_bag: [0.2, 0.4, 0.65, 0.9],
  p_occupancy: [0.5, 0.7, 0.85, 1.0],
  door_headway: [1.0, 1.5, 2.0, 3.0],
  bag_scale: [0.5, 1.0, 1.5, 2.0],
  bins_elsewhere: [0.0, 0.28, 0.5],
  gap_distance: [0.5, 1.0, 2.0],
  party_share: [0.0, 0.33, 0.6],
  p_pause: [0.0, 0.15, 0.3, 0.5],
};
function withOverride(cfg, param, value) {
  if (param === "bag_scale") return { ...cfg, bag_time: cfg.bag_time.map(([p, v]) => [p, v * value]) };
  if (param === "bins_elsewhere") {
    const total = cfg.bin_offsets.filter(([o]) => o !== 0).reduce((s, [, w]) => s + w, 0);
    return { ...cfg, bin_offsets: cfg.bin_offsets.map(([o, w]) => [o, o === 0 ? 1 - value : w / total * value]) };
  }
  if (param === "party_share") {
    const total = cfg.p_pair + cfg.p_trio;
    return { ...cfg, p_pair: cfg.p_pair / total * value, p_trio: cfg.p_trio / total * value };
  }
  return { ...cfg, [param]: value };
}

function runAll(cfgs) {
  // cfgs: list of configs; returns list of arrays of K totals (same seeds for all)
  const seeds = runSeeds(K, SEED);
  const jobs = [];
  cfgs.forEach((cfg, ci) => seeds.forEach((s, si) => jobs.push({ ci, si, job: [cfg, s] })));
  const out = cfgs.map(() => new Array(K));
  return new Promise((resolve) => {
    let finished = 0;
    for (let t = 0; t < THREADS; t++) {
      const mine = jobs.filter((_, i) => i % THREADS === t);
      const w = new Worker(__filename, { workerData: { jobs: mine.map((j) => j.job) } });
      let i = 0;
      w.on("message", (total) => { const j = mine[i++]; out[j.ci][j.si] = total; });
      w.on("exit", () => { if (++finished === THREADS) resolve(out); });
      w.on("error", (e) => { console.error(e); process.exit(1); });
    }
  });
}

const mean = (xs) => xs.reduce((a, b) => a + b, 0) / xs.length;

(async () => {
  const rows = [["sweep", "value", "p_yield", "mean_total", "diff", "ci_lo", "ci_hi", "mean_exit_or_diff"]];
  const base = DEFAULT_CONFIG;
  console.log(`Sweep on the JS port: K=${K}, seed=${SEED}, ${THREADS} threads`);

  const pys = [0.0, 0.1, 0.25, 0.5, 0.75, 1.0];
  const curve = await runAll(pys.map((py) => ({ ...base, p_yield: py })));
  pys.forEach((py, i) => {
    const [d, lo, hi] = pairedCI(curve[i], curve[0], SEED);
    console.log(`p_yield ${py.toFixed(2)}  mean ${mean(curve[i]).toFixed(1)} s  Δ vs 0 ${d.toFixed(1)} [${lo.toFixed(1)}, ${hi.toFixed(1)}]`);
    rows.push(["p_yield", py, py, mean(curve[i]), d, lo, hi, ""]);
  });

  for (const [param, values] of Object.entries(SWEEPS)) {
    const cfgs = [];
    for (const v of values) {
      const c = withOverride(base, param, v);
      cfgs.push({ ...c, p_yield: 1.0 }, { ...c, p_yield: 0.0 });
    }
    const res = await runAll(cfgs);
    values.forEach((v, i) => {
      const court = res[2 * i], aisle = res[2 * i + 1];
      const [d, lo, hi] = pairedCI(court, aisle, SEED);
      console.log(`${param}=${v}  courtesy ${mean(court).toFixed(1)} s  aisle ${mean(aisle).toFixed(1)} s  Δ ${d.toFixed(1)} [${lo.toFixed(1)}, ${hi.toFixed(1)}] (${(d / mean(aisle) * 100).toFixed(1)}%)`);
      rows.push([param, v, "", mean(court), d, lo, hi, ""]);
    });
  }
  if (CSV) {
    fs.writeFileSync(CSV, rows.map((r) => r.join(",")).join("\n") + "\n");
    console.log(`Results saved to ${path.resolve(CSV)}`);
  }
})();
