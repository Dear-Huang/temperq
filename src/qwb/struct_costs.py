"""Lagged quoted half-spread estimates, separate from fees and impact pressure."""
from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd

from . import netcost
from .portfolio.reference import Fees


def tick_half_spread_bp(price):
    """One-tick quoted-spread component / 2, not a realized execution-cost floor."""
    if not math.isfinite(price) or price <= 0:
        raise ValueError('价位地板输入价格必须为正且有限')
    from ashare_tcost import tick_floor_bp
    value = float(tick_floor_bp(price)) / 2.
    if not math.isfinite(value):
        raise ValueError('价位地板结果超出有限数值范围')
    return value


@dataclass(frozen=True)
class StructFees(Fees):
    half_spread_bp:float=0.
    source_month:str=''
    calibration_id:str=''

    def __post_init__(self):
        super().__post_init__()
        if not math.isfinite(self.half_spread_bp) or self.half_spread_bp<0:
            raise ValueError('STRUCT 半价差估计必须非负且有限')

    def costs(self,notional,side,day):
        return {**super().costs(notional,side,day),'struct_spread':notional*self.half_spread_bp/10000}

    def metadata(self):
        return {'cost_model':'struct_v1','estimated_half_spread_bp':self.half_spread_bp,
            'cost_inputs_month':self.source_month,'cost_calibration_id':self.calibration_id or 'unspecified',
            'cost_scope':'quoted_half_spread_estimate_not_realized_impact'}


def estimates(panel):
    # Lagged calibration fitted on target months through 2023-12. This is a
    # retrospective cost estimate, not evidence of a model known before 2024.
    return netcost.struct_half_spread_bp(panel)


def fees_for_day(spread,day,assets,fees):
    month=str(pd.Timestamp(day).to_period('M')-1)
    result={}
    for code in assets:
        value=float(spread.at[day,code])
        if not math.isfinite(value) or value<0:
            raise ValueError(f'{day:%Y-%m-%d} {code} 缺少上月 STRUCT 输入，不能按零成本继续')
        result[code]=StructFees(fees.commission_bp,fees.minimum_commission,fees.slippage_bp,value,month,
                               spread.attrs.get('struct_calibration_id',''))
    return result
