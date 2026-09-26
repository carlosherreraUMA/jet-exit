// Node check that web/model.js reproduces the Python reference runs.
// Usage: node web/parity_test.js
const { Sim, runSeeds } = require("./model.js");
const { DEFAULT_CONFIG, PARITY } = require("./data.js");

let ok = 0;
for (const c of PARITY) {
  const sim = new Sim({ ...DEFAULT_CONFIG, ...c.overrides }, c.seed);
  sim.run();
  let cs = 0;
  for (const p of sim.pax) cs = (cs + (p.pid + 1) * p.tExit) % 2147483647;
  const good = sim.tick === c.ticks && cs === c.checksum && sim.pax.length === c.n;
  ok += good;
  console.log(good ? "OK " : "BAD", JSON.stringify(c.overrides), `ticks ${sim.tick}/${c.ticks}`, `checksum ${cs}/${c.checksum}`);
}
console.log(`${ok}/${PARITY.length} identical`);
const t0 = Date.now();
for (const s of runSeeds(20, 1)) new Sim({ ...DEFAULT_CONFIG, p_yield: 1 }, s).run();
console.log(`${((Date.now() - t0) / 20).toFixed(1)} ms per full courtesy run`);
process.exit(ok === PARITY.length ? 0 : 1);
