# Data to calibrate the model

Every model parameter is a slot for measured data. `python simulate_deboarding.py params` lists
them all with their current value, unit, where the value comes from and how to measure it. This
document collects what the sources found so far say, and what is still missing.

Distributions are written as quantile tables `((p, value), ...)`, so a measured sample goes in
directly: sort the values and take, for example, the 0, 5, 10, 25, 50, 75, 90, 95 and 100th
percentiles. They are changed with `--set`, for example:

```
python simulate_deboarding.py --set 'bag_time=[[0,2],[0.5,6],[0.9,12],[1,25]]'
```

## What the sources say

| Quantity | Value | Source | Use in the model |
|---|---|---|---|
| Deboarding rate through one door | 15-17 pax/min (observed average); 20 pax/min quoted as the industry average | Wald, Harmon and Klabjan 2014, as cited by CALM | Calibration target for courtesy |
| Total time, A319/A320 at ≥ 80% occupancy | 6:27 to 13:00 min | Vink et al. 2019 (field study) | Validation range |
| Total time, 144-seat A320 | 8-10 min | CALM (Namilae et al.), citing observed data | Validation range |
| Stowing hand luggage when boarding | Weibull, shape 1.7 and scale 16 s (mean ≈ 14 s, 323 measurements) | Schultz 2018, Aerospace 5(1):27 | Shape of `bag_time` (taking a bag down is usually faster than stowing it) |
| Hand luggage items per passenger | 0 / 1 / 2 at 25 / 50 / 25% | Schultz et al. 2023, JATM 106 | Guides `p_bag` |
| Free walking speed in the aisle | 0.8 m/s | Schultz et al. 2023 | Median of `walk_speed` |
| General pedestrian speed | 1.1-1.3 m/s (σ 0.2) | CALM | Upper bound of `walk_speed` |
| Taking luggage down when deboarding | 5-12 s (assumed, not measured) | CALM | Cross-check for `bag_time` |
| Bags stored further back | Force people to walk against the flow; the main bottleneck | Vink et al. 2019 | Reason for `bin_offsets` and walking back |
| No hand luggage | −80% deboarding time | Vink et al. 2019 | Check of the sensitivity to `p_bag` |

An important observation: the published models reviewed (CALM, Wald et al.) are calibrated on the
total time only. None of them measures the detailed deboarding times (taking a bag down, standing
up, which gaps people accept). Those data still have to be collected.

## What still needs measuring, by impact

1. **`bag_time`**: time from opening the bin until the bag is out. This parameter weighs the most.
2. **`p_prefetch`**: how many passengers with a bag take it down before stepping into the aisle
   (standing in their row) and how many do it in the aisle.
3. **`bin_offsets`**: seat row against the row of the bin holding their bag.
4. **`door_headway`**: exits per minute while there is a continuous queue at the door. It decides
   how much everything else matters.
5. **`p_yield`, `gap_distance`**: in what share of rows the aisle stops to let people out, and
   which gaps people accept when stepping out without being let in.
6. **`s0`, `headway`, `start_gap`, `body_depth`**: people per metre in a stopped queue and how
   fast a queue gets moving (measured as how long the Nth person takes to move after the first).
7. **`p_pause`, `pause_time`**: how many people stop once on the way to the door, and for how
   long. Stops hurt aisle first much more than courtesy, so this matters for the conclusion.
8. **`p_pair`, `p_trio`**: neighbours who travel together and wait for each other.
9. **`prep_time`, `stand_time`, `lateral_time`, `step_in_time`**: times inside the row.

## Measuring it on video

### Tool and pipeline

1. Open `web/annotate.html` (also linked from the simulation), load the video and fill in the
   flight details. The video plays in the browser and is never uploaded.
2. Pick a passenger on the seat map, or type row and letter (`12c`), and mark their events with
   one key as the video plays: `S` stands up, `E` at the row edge, `A` steps into the aisle,
   `Y`/`N` someone yields or nobody yields, `B` opens the bin (with the bin row), `D` bag down,
   `R` walks back, `W` starts walking, `H` halts on the way, `M` moves on again, `X` exits.
   To mark people travelling together, select one, then the other, and press `G`.
   For the whole flight: `O` seatbelt sign off, `U` someone you are not following exits.
3. Copy the CSV and calibrate:
   ```
   python calibrate.py flight1.csv flight2.csv --out calibrated.json --validate
   python simulate_deboarding.py --config calibrated.json
   ```
   `calibrate.py` only changes parameters with at least 8 samples (across flights), keeps the
   rest as they are, and says how many samples are missing.

The pipeline is checked on simulated flights: `python calibrate.py --synthetic 3` annotates three
simulated flights with known parameters and recovers them (median `bag_time` 7.1 s against
7.3 s; `p_prefetch` 0.78 against 0.80; `p_bag` 0.66 against 0.65). Three measurement details came
out of that check:

- **Bag down** is when the bag is out of the bin, even if the person is still holding it while
  waiting for room. If someone starts taking it down in the row and finishes in the aisle, that
  measurement is not used for `bag_time`.
- **`p_prefetch`** can only be observed for people who wait at the row edge for at least 3 s: if
  someone steps into the aisle without waiting, we cannot tell whether they would have taken
  their bag down from the row.
- **`N` (nobody yields)** is only marked when someone was coming down the aisle. If nobody was
  coming, mark neither `Y` nor `N`.

### What to record

- **Ideal footage**: a video from the front of the cabin looking back, or from the last row
  looking towards the door, covering the whole deboarding. Searching YouTube for deboarding
  time-lapses filmed by crews may turn some up; filming from your own seat also works.
- **Reference case**: the time-lapse filmed by a WestJet crew (Fort McMurray - Calgary) shows a
  strict courtesy deboarding, row by row, with experienced flyers. It is a good validation case
  for `p_yield = 1` if its time factor is known.
- **No need to annotate everyone**: following 10-20 people per video carefully, plus every exit
  through the door (`U`), already gives useful samples.
- **Validation**: with `--validate`, `calibrate.py` compares each flight's observed total time
  (from the sign going off to the last exit) with the distribution of the calibrated model.
- **Privacy**: do not publish videos in which passengers can be recognised. The CSV only
  contains times and seat numbers.

## References

- Schultz, M. (2018). Field Trial Measurements to Validate a Stochastic Aircraft Boarding Model.
  *Aerospace* 5(1):27. https://doi.org/10.3390/aerospace5010027
- Schultz, M. et al. (2023). A combined optimization–simulation approach for modified
  outside-in boarding. *Journal of Air Transport Management* 106:102258.
  https://www.unibw.de/lvk/publications/pdf/2022_jatm_boarding_bag.pdf
- Namilae, S. et al. Constrained Linear Movement Model (CALM): Simulation of passenger movement
  in airplanes. https://arxiv.org/pdf/1910.05749
- Wald, A., Harmon, M., Klabjan, D. (2014). Structured deplaning via simulation and
  optimization. *Journal of Air Transport Management* 36.
  https://www.sciencedirect.com/science/article/abs/pii/S0969699714000027
- Hiemstra-van Mastrigt, S., Ottens, R., Vink, P. (2019). Identifying bottlenecks and designing
  ideas and solutions for improving aircraft passengers' experience during boarding and
  disembarking. *Applied Ergonomics* 77. https://pubmed.ncbi.nlm.nih.gov/30832774/
