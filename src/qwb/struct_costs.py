"""Lagged quoted half-spread estimates, separate from fees and impact pressure."""
from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd

from . import netcost
from .portfolio.reference import Fees


@dataclass(frozen=True)
class StructFees(Fees):
    half_spread_bp:float=0.
    source_month:str=''

    def __post_init__(self):
        super().__post_init__()
        if not math.isfinite(self.half_spread_bp) or self.half_spread_bp<0:
            raise ValueError('STRUCT 半价差估计必须非负且有限')

    def costs(self,notional,side,day):
        return {**super().costs(notional,side,day),'struct_spread':notional*self.half_spread_bp/10000}

    def metadata(self):
        return {'cost_model':'struct_v1','estimated_half_spread_bp':self.half_spread_bp,
            'cost_inputs_month':self.source_month,'cost_scope':'quoted_half_spread_estimate_not_realized_impact'}


def estimates(panel):
    # Coefficients fitted using 2021–2023 L2. This implementation is a
    # retrospective cost estimate, not evidence of a model known before 2024.
    return netcost.struct_half_spread_bp(panel)


def fees_for_day(spread,day,assets,fees):
    month=str(pd.Timestamp(day).to_period('M')-1)
    result={}
    for code in assets:
        value=float(spread.at[day,code])
        if not math.isfinite(value) or value<0:
            raise ValueError(f'{day:%Y-%m-%d} {code} 缺少上月 STRUCT 输入，不能按零成本继续')
        result[code]=StructFees(fees.commission_bp,fees.minimum_commission,fees.slippage_bp,value,month)
    return result
