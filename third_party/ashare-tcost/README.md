# ashare-tcost

Transaction-cost estimates for China A shares, computed from public daily data.

The model is called STRUCT. It treats the quoted spread as the larger of two parts: the one-tick floor that the 0.01-yuan price increment imposes, and a log-linear liquidity component:

```
spread_bp = max(100 / price,  exp(b0 + b1·log amount + b2·log vol + b3·log price))
```

Out of sample (2024-01 to 2026-06), STRUCT ranks stocks by measured L2 quoted spread with ρ = 0.913. It recovers 88% of the most expensive 10% of names. Common proxies do worse: Amihud reaches 0.52 / 24%, and Corwin–Schultz −0.01 / 9%. See [VALIDATION.md](VALIDATION.md).

## Install

```
pip install -e .
```

## Use

```python
import ashare_tcost as tc

# daily: code, date, high, low, close, close_raw, amount (yuan), ret (decimal)
monthly = tc.monthly_features(daily)          # one row per stock-month
monthly["cost_bp"] = tc.struct_spread(monthly)

# with your own measured spreads, refit the liquidity part
beta = tc.fit_struct(my_months)               # needs spread_bp, amount, vol, price
monthly["cost_bp"] = tc.struct_spread(monthly, beta)
```

`cost_bp` is the full quoted spread in basis points. A round trip of small orders pays about one spread; a one-way trade pays about half.

## What it is not

It does not model price impact for large orders, and it does not cover markets with a different tick grid. The default coefficients were fitted on 2021–2023 A-share data.

## Citation

Paper: *When the Tick Binds* (SSRN, forthcoming). Please cite the paper if you use the model.

## License

MIT
