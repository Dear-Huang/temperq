# ashare-tcost

Transaction-cost estimates for China A shares, computed from public daily data.

The model is called STRUCT. It treats the quoted spread as the larger of two parts: the one-tick floor that the 0.01-yuan price increment imposes, and a log-linear liquidity component:

```
spread_bp = max(100 / price,  exp(b0 + b1·log amount + b2·log vol + b3·log price))
```

The default is the **lagged design**: month t−1 daily-data features estimate month t's quoted spread. The archived paper validation (2024-01 to 2026-06) reports ρ = **0.904** and **85.6%** overlap with the most expensive 10% of stocks. On the matched sample, lagged inverse price gives 0.718 / 84.5%. These are ranking metrics for quoted spreads, not realized execution-cost accuracy. See [VALIDATION.md](VALIDATION.md) for sample selection and reproduction limits.

Version 0.2.0 uses `DEFAULT_BETA = [10.4316, -0.3758, 0.4983, 0.1689]`, in the order intercept, log amount, log volatility, log raw price. The historical same-month fit is available explicitly as `SAME_MONTH_BETA`; its 0.913 / 88.0% result is not the default deployment claim.

## Install

```
pip install -e .
```

## Use

```python
import ashare_tcost as tc

# daily: code, date, high, low, close, close_raw, amount (yuan), ret (decimal)
monthly = tc.monthly_features(daily)          # one row per stock-month
forecast = tc.lag_monthly_features(monthly)   # source m -> target m+1, once
forecast["cost_bp"] = tc.struct_spread(forecast)
# forecast.ym is the target month; forecast.feature_ym is the source month
```

To refit, prepare a separate training frame with aligned outcomes and an explicit training cutoff:

```python
# Optional refit: first join lagged features to your measured target outcomes,
# restrict training dates, and supply TARGET-month eligibility in target_n_ticks.
# target_n_ticks = target measured spread_bp * target raw monthly price / 100
beta = tc.fit_struct(aligned_training_rows, target_n_ticks="target_n_ticks")
forecast["refitted_cost_bp"] = tc.struct_spread(forecast, beta)
```

`struct_spread` evaluates supplied rows; it does **not** shift features. `lag_monthly_features` relabels each source month to the next calendar month, preserves `feature_ym`, and rejects already aligned inputs. Missing calendar months stay missing. A caller that already aligns previous-month data must not apply another lag. The workbench performs this alignment itself.

`cost_bp` is the **full quoted spread** in basis points. Half is a per-side small aggressive-order cost proxy; auctions, passive orders, queue effects and impact require separate execution analysis. The tick-only quantity remains `100/price` bp full spread or `50/price` bp half spread, independent of the liquidity coefficients.

## What it is not

It does not model price impact for large orders or markets with a different tick grid. The default coefficients were fitted on target months through 2023-12, using previous-month features. Validation requires eligible L2 observations in both months; this selection is not an all-stock deployment guarantee. The library is an estimation tool, not a real-time data feed or an execution-cost lower-bound certificate.

`monthly_features` fills missing volatility only from the same source month's cross-section; an entirely missing source-month volatility stays missing. The archived research used a pooled median. That preprocessing correction and different supplied panels can change a fresh refit; the archived ranking numbers have not been recomputed for this package release.

Updating the full model changes estimates for new analyses. The workbench records a calibration ID and fingerprints model source in frozen strategies: saved reports remain historical, and old frozen strategies require a new version before continuing under changed implementation. No historical ledger is rewritten.

## Citation

Paper: *When the Tick Binds* (SSRN, forthcoming). Please cite the paper if you use the model.

## License

MIT
