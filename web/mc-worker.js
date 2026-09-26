// Monte Carlo worker: runs a batch of simulations off the main thread.
// Message in:  { id, jobs: [[configOverrides, seed], ...], base }
// Message out: { id, results: [metrics, ...] } per finished job, in order.
importScripts("model.js");

self.onmessage = (e) => {
  const { id, jobs, base } = e.data;
  for (let i = 0; i < jobs.length; i++) {
    const [overrides, seed] = jobs[i];
    const sim = new self.Deboarding.Sim({ ...base, ...overrides }, seed);
    sim.run();
    self.postMessage({ id, index: i, metrics: sim.metrics() });
  }
  self.postMessage({ id, done: true });
};
