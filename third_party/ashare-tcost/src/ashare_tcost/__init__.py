"""ashare-tcost: transaction-cost (quoted spread) estimates for China A shares
from public daily data.

    import ashare_tcost as tc
    monthly = tc.monthly_features(daily)        # daily OHLC + amount -> stock-month
    monthly["spread_bp"] = tc.struct_spread(monthly)

STRUCT = max(tick floor, liquidity component): the 0.01-yuan minimum price
increment puts a floor of 100/price bp under the quoted spread, and above the
floor the spread follows a log-linear liquidity model. Coefficients are fitted
on L2 quoted spreads (2021-01..2023-12, stock-months above 1.3 ticks); see
VALIDATION.md for the out-of-sample check.
"""

from .model import (DEFAULT_BETA, TICK_YUAN, fit_struct, struct_spread,  # noqa: F401
                    tick_floor_bp)
from .proxies import amihud, corwin_schultz, monthly_features, vov, zero_share  # noqa: F401
from .evaluate import rank_agreement  # noqa: F401

__version__ = "0.1.0"
