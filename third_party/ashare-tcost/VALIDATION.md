# Validation

## Data

- Measured cost: monthly mean of the daily L2 relative quoted spread (bp), from the author's private A-share L2 archive. Stock-months need L2 coverage ≥ 80% and at least 10 valid days. Codes are restricted to the main board, ChiNext and STAR. Stock-months below 0.98 ticks are dropped. The L2 data is not distributed.
- Daily inputs: OHLC, raw close, traded value and return from a standard daily panel.
- The original eligible same-month panel spans 2021-01 to 2026-06 with 281,288 stock-months and 5,354 stocks; 54% have a spread of at most 1.3 ticks. This is not the size of the lagged matched sample.

## Protocol

- Default design: features from calendar month t−1 predict L2 quoted spread in month t. The tick component also uses the source month's raw price.
- Fit target window: 2021-02 to 2023-12, 65,629 unbound observations. The >1.3-tick eligibility test uses **target-month** observed spread and raw price. It is a training-sample filter, not an input to live prediction.
- Test target window: 2024-01 to 2026-06, 30 months. Of 136,572 eligible target stock-months, 129,164 match an eligible previous-month row, covering 5,096 stocks. Both months must pass the research eligibility filters; this is a selected matched sample.
- Default coefficients, rounded as archived: `[10.4316, -0.3758, 0.4983, 0.1689]`; calibration ID `struct_lagged_2021_2023_v1`.
- Metrics, computed within each month and then averaged over months:
  - Spearman ρ between the estimate and the measured spread.
  - Overlap between the top 10% most expensive names by estimate and the top 10% by measured spread.

## Archived Paper Results: Lagged Design (Default)

| Estimator | ρ | Top-10% overlap |
|---|---|---|
| **STRUCT, t−1 inputs** | **0.904** | **85.6%** |
| 1/price, t−1 | 0.718 | 84.5% |
| Amihud, t−1 | 0.509 | 22.8% |
| −log traded value, t−1 | 0.502 | 19.9% |
| Corwin–Schultz, t−1 | −0.023 | 8.8% |

These numbers and coefficients were checked against the paper reproduction's
`lagged_validation_summary.json` on 2026-10-27. This release did not rerun the
private 2024–2026 panel. Reading an archived aggregate is not a fresh validation.

## Reproduction And Historical Design

`scripts/reproduce_paper.py <l2-monthly.parquet> <daily.parquet>` now defaults to
the lagged design. It joins source and target calendar months explicitly, uses
target-month tick eligibility, fits training targets only, and reports differences
from the archived paper metrics. `--design same_month` explicitly selects the
historical contemporaneous design below; it is not the default calibration.

Historical same-month recomputation recorded on 2026-10-21:

| | Package | Paper |
|---|---|---|
| β (const, log amount, log vol, log price) | 10.8731, −0.3867, 0.5668, 0.1720 | same; max abs diff 4e-5 |
| STRUCT ρ | 0.9131 | 0.913 |
| STRUCT top-10% overlap | 87.97% | 88.0% |

Those historical coefficients remain available as `SAME_MONTH_BETA`. The former
headline numbers must not be quoted as the lagged deployment result.

The reproduction script consumes user-supplied private data; it does not bundle
the original panel. Package preprocessing now imputes missing volatility from
the source-month cross-section, while the archived research used a pooled median.
Coverage, preprocessing and rounded coefficients can affect a fresh run. No
synthetic unit test is evidence that the original empirical metrics were reproduced.

The packaged Corwin–Schultz comparator is now dated at the end of its two-day
window (previous day and current day). The former forward pair could borrow the
first day of the next month for a month-end estimate. This timing correction
does not change STRUCT inputs or coefficients. The archived comparator metrics
above have not been rerun with this correction.

`monthly_features` builds source-month features. `lag_monthly_features` moves their
labels to the next calendar month once; `struct_spread` itself does not shift.
The workbench aligns its matrix estimates by one calendar month independently.

## Limits

- The target is the **full quoted** spread. Half is a small aggressive-order per-side proxy, not a lower bound on realized execution cost. Auctions/passive execution and impact need separate analysis.
- The coefficients reflect the 2021–2023 A-share regime and the 0.01-yuan tick. If you have measured spreads, refit with `fit_struct`.
- Results above 1.3 ticks depend on a log-linear form. Within each month, the floor component does most of the ranking work for low-priced stocks.
- Daily-range estimators such as Corwin–Schultz show near-zero or negative correlation with measured A-share spreads. Price limits and the tick grid distort the high–low range, so these estimators are included only as a comparison.
