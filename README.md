# Who yields the aisle?

A simulation of deboarding a single-aisle aircraft (A320, 30 rows 3-3) to answer one question:
**would we get off sooner if people already standing in the aisle did not stop to let each row
out, and those still seated waited for a gap instead?**

The rule is a single parameter, `p_yield`: the probability that someone walking down the aisle
stops to let a row out. `1` is the usual courtesy; `0` is aisle first.

## Current result

Over 100 simulated planes with the default parameters, courtesy takes 12:34 and aisle first
6:48: courtesy is 85% slower. The advantage holds for every parameter tested and shrinks mostly
when the door is slow (3 s per passenger: +35%). If just one walker in ten yields, about half of
the advantage is gone.

Many parameters are still estimates. The model is calibrated against the observed overall rate
(15-17 passengers per minute), but detailed measurements are missing; see [DATA.md](DATA.md)
and the data section below.

## What is here

| File | What it is |
|---|---|
| `simulate_deboarding.py` | The model and the Monte Carlo CLI (Python, no dependencies). |
| `calibrate.py` | Turns annotations of real videos into model parameters, and validates. |
| `animate_deboarding.py` | Terminal animation of both rules side by side. |
| `web/index.html` | Live simulation, charts and an in-browser Monte Carlo. |
| `web/cabin3d.html` | Both cabins in 3D (three.js). |
| `web/annotate.html` | Tool to annotate videos of real deboardings. |
| `web/model.js` | JavaScript port of the model, bit-identical to the Python one. |
| `sweep.js` | Sensitivity sweep on the JS port (about 20x faster). |
| `DATA.md` | Sources, parameters still to be measured, and the measurement protocol. |

## Usage

```
python simulate_deboarding.py --k 300 --seed 1        # courtesy against aisle first
python simulate_deboarding.py params                  # every parameter, with its source
python simulate_deboarding.py --set p_bag=0.4          # change any parameter
python animate_deboarding.py --rows 12                # terminal animation
node sweep.js --k 100 --csv sweep.csv                 # sensitivity sweep
```

For the web pages, serve the `web/` folder (for example `python -m http.server -d web`) and
open `http://localhost:8000`.

## The model in brief

- A continuous aisle in metres. Each person has a body depth, a walking speed and limited
  acceleration, and keeps a distance to the person ahead. Queues restart as a wave.
- In each row the aisle seat leaves first; the others stand up and shuffle sideways.
- Bags can be above the passenger's row, ahead of it or behind it. Someone whose bag is behind
  and out of reach waits for the aisle to clear, reserves it and walks against the flow.
- Every parameter lives in `Config`, with its unit, source and how to measure it.
  Distributions are quantile tables, so measured data can be pasted in as they are.

The full description is at the top of `simulate_deboarding.py`.

## Contributing data

What would improve the model most is measurements of real deboardings: how long it takes to take
a bag down, how many do it from their row, where the bag is relative to the seat, when people
yield. If you have a video where the aisle is visible:

1. Annotate it with `web/annotate.html` (the video never leaves your computer).
2. Copy the CSV and see what can be estimated: `python calibrate.py your-flight.csv`.
3. Contribute the CSV (not the video): it is anonymous and only contains times and seat numbers.

## Tests

```
python -m pytest test_simulation.py test_calibrate.py
node web/parity_test.js            # the JS port reproduces Python bit for bit
```

After changing the model, `python export_parity.py` regenerates `web/data.js` (default
configuration and reference runs), and `node web/parity_test.js` checks parity.

## License

[MIT](LICENSE) © 2026 Carlos. You may use, modify and redistribute the code freely, as long as
you keep the copyright notice and the license. If you use it in a piece of work, article or
presentation, a mention of the project is appreciated.
