# Validation

## Data

- Measured cost: monthly mean of the daily L2 relative quoted spread (bp), from the author's private A-share L2 archive. Stock-months need L2 coverage ≥ 80% and at least 10 valid days. Codes are restricted to the main board, ChiNext and STAR. Stock-months below 0.98 ticks are dropped. The L2 data is not distributed.
- Daily inputs: OHLC, raw close, traded value and return from a standard daily panel.
- Sample: 2021-01 to 2026-06, 281,288 stock-months, 5,354 stocks. 54% of stock-months have a spread of at most 1.3 ticks.

## Protocol

- Fit window: 2021-01 to 2023-12. The liquidity part is fitted only on stock-months above 1.3 ticks.
- Test window: 2024-01 to 2026-06, out of sample (30 months, 136,572 stock-months).
- Metrics, computed within each month and then averaged over months:
  - Spearman ρ between the estimate and the measured spread.
  - Overlap between the top 10% most expensive names by estimate and the top 10% by measured spread.

## Results (test window)

| Estimator | ρ | Top-10% overlap |
|---|---|---|
| **STRUCT** | **0.913** | **88.0%** |
| 1/price (tick floor alone) | 0.717 | 86.1% |
| Amihud | 0.520 | 23.7% |
| −log traded value | 0.495 | 21.0% |
| Corwin–Schultz | −0.007 | 9.2% |

`scripts/reproduce_paper.py` reproduces these numbers with this package. Run on 2026-10-21:

| | Package | Paper |
|---|---|---|
| β (const, log amount, log vol, log price) | 10.8731, −0.3867, 0.5668, 0.1720 | same; max abs diff 4e-5 |
| STRUCT ρ | 0.9131 | 0.913 |
| STRUCT top-10% overlap | 87.97% | 88.0% |

A lagged variant uses month t−1 features to predict month t spreads, fitted through 2023-12. It gives ρ 0.904 and overlap 85.6% (paper recomputation, `recompute_lagged_validation.py`).

## Limits

- The target is the **quoted** spread, which measures half-spread cost for small orders. Price impact for larger orders is a separate quantity and is not modelled here.
- The coefficients reflect the 2021–2023 A-share regime and the 0.01-yuan tick. If you have measured spreads, refit with `fit_struct`.
- Results above 1.3 ticks depend on a log-linear form. Within each month, the floor component does most of the ranking work for low-priced stocks.
- Daily-range estimators such as Corwin–Schultz show near-zero or negative correlation with measured A-share spreads. Price limits and the tick grid distort the high–low range, so these estimators are included only as a comparison.
