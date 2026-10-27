# B0–B5 training comparison: frozen development protocol

Registered 2026-10-26 before new model fitting or outer-fold results.

## Scope

Train all six groups on the existing hash-selected 240-name development subset,
2018–2023 only. Outer folds are calendar years 2021, 2022, 2023; seeds 0, 1, 2.
This is development evidence, not untouched confirmation. No 2026 rows are read.
No API calls or real accounts are involved. Budget: at most 24 process CPU hours.

Implementation attempts: 001 failed in array preparation before fitting. 002
completed one fold/seed; before reviewing outer metrics, strict raw-share target
validation exposed float32 representation of the 10% cap. Attempt 003 repeats
all fits and clips exported float64 weights to the unchanged strict caps.
Previous attempt outputs are retained; no method/hyperparameter was changed.

Use 32 causal price/amount features plus six fixed market summaries for every
group. Eligibility: 60 observed sessions, non-ST, bottom 5% daily amount excluded,
finite features. No future availability filter on prediction rows. Labels are
next-open to open at offsets 6, 11, 21 (5/10/20 sessions). The last 60 mature
training dates form inner validation; exclude training labels ending on/after
validation begins. Refit only labels ending strictly before the outer year.
Use the longest horizon's maturity mask for all groups.

## Fits

- B0: five-day rank LightGBM. Inner daily RankIC chooses between 63 leaves /
  minimum leaf 1000 and 31 / 500, and up to 500 rounds (patience 30).
- B1: the same LightGBM search for each of three horizons. Inner validation
  selects single 5/10/20-day or equal-three calibrated daily-return forecasts.
- B2/B4: 64-wide, two-layer MLP / latent-token cross-stock context network;
  three ranked-label outputs, AdamW, learning rate .001, weight decay .001,
  at most 25 epochs, daily batches of 16 dates, patience 5, inner five-day IC.
  Both structures share the same fixed market summaries and output recipe.
- B3/B5: warm-start respective B2/B4; exploratory net-utility fine-tuning,
  learning rate .0001, at most 5 epochs. Inner validation selects epoch,
  including epoch zero; refit pretraining then repeat selected fine-tuning epochs.
  Loss: negative mean log net return + 5 * variance + .01 prediction MSE.
  Chronological 20-session episodes, rebalance each five sessions; realized
  open-to-open returns are counted once, weights drift between decisions.
  Historical labels/episodes must mature before the relevant fitting cutoff.

The old GRU/QP failed Stage A qualification. B3/B5 here explicitly test a
different, simpler piecewise differentiable top-20 softmax head (temperature .2,
10% name cap, 95% target exposure). This is an exploratory amendment, not a
claim that the original QP is repaired. Record gradients, parameter changes,
action changes, and proxy/execution mismatch; lack of exact-account qualification
prevents promotion regardless of surrogate performance.

## Portfolio and controls

All arms start each outer year with 1 million cash, decide at close, execute
next open, rebalance every five sessions. B0 uses equal-weight top 20, 95%
exposure, 10% name cap. Add B0-cost to separate allocator from forecast effects.
B1–B5 and B0-cost use the same calibrated cost-aware top-20 softmax allocator.
For each horizon, regress inner OOS raw returns on inner OOS model score using
only validation labels mature before the outer cutoff; truncate negative slopes
to zero. Calibrated forecasts are divided by their horizon. Penalize new names
by one-way estimated commission/spread/half round-trip stamp over five sessions;
names in the preceding intended basket receive no entry penalty. This uses a
policy memory rather than claiming access to perfectly filled holdings. No
threshold search on outer outcomes. Neural utility episodes use their own
drifted continuous holdings; this execution mismatch is reported as a limitation.

Primary exploratory accounting uses the existing independent fractional adjusted
unit ledger, with STRUCT previous-month spread, 3bp commission, execution-date
stamp duty, price limits and status. Repeat with +10bp one-way slippage. It has
no integer lots, T+1 lot ledger or minimum commission and may carry missing
marks. **It cannot establish deployable economic superiority.** Attempt an
independent raw-share STRUCT replay and retain its first lifecycle/data failure;
never drop offending dates, infer dividends from adjustment factors, or silently
substitute zero spread. STRUCT coefficients themselves use 2021–2023 L2:
historical cost results are retrospective estimates.

## Decision rule

Primary endpoint stays annualized daily utility, 252 * [mean(log(1+r)) -
5 * population variance(r)]. Report CAGR, drawdown, volatility, exposure,
turnover, cost, IC, per-fold and per-seed results; no seed treated as a date.
The mean of the three seed paths is the algorithm-level daily path (not an
executed ensemble). Paired within-year moving-block bootstrap, block 20,
4000 draws, seed 20261026. Familywise intervals cover six comparisons to B0
and B3–B2, B4–B2, B5–B4, B5–B3. Repeat economic stress, report exposure and
turnover differences. Do not mistake less exposure/turnover for predictive alpha.

A point-estimate leader is not a confirmed winner. Require positive corrected
paired interval, consistent folds/seeds, stress robustness, and exact execution
qualification before claiming an economic breakthrough. Preserve failed jobs,
selection logs, predictions, checkpoints, source/data hashes and CPU timings.
Do not change this recipe after viewing outer results; report implementation
errors and any reruns explicitly.
