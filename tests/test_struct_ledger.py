from dataclasses import replace

import numpy as np
import pytest

from qwb import strategy,struct_costs,strategy_workspace as w
from qwb.portfolio import reference,struct_reference
from test_daily_strategy import D0,O1,C1,O2
from test_strategy_workspace import setup,create


def execute(executor,fees,*,cash=2005,holdings=None,weights=None,extra=None):
    state=strategy.account(cash,holdings or [],{'600000.SH':10},observed_at=D0,snapshot_id='test',locked_until=O2)
    target,assets,universe=strategy.target_for(state,weights if weights is not None else {'600000.SH':1},execution_at=O1,recipe_hash='test')
    bar=strategy.session_bar('600000.SH',open_price=10,close_price=10,upper=11,lower=9)
    return executor(state,target,(bar,),open_at=O1,close_at=C1,next_open_at=O2,
        decision_assets=assets,universe_id=universe,protocol_id='test',fees=fees,**(extra or {}))


def test_zero_spread_keeps_legacy_execution_and_spread_changes_cash_affordability():
    base=reference.Fees()
    old,a=execute(reference.execute_session,base)
    current,b=execute(struct_reference.execute_session,base,extra={'fees_by_asset':{'600000.SH':struct_costs.StructFees(half_spread_bp=0)}})
    assert current==old and b['total_cost']==a['total_cost']
    spread=struct_costs.StructFees(half_spread_bp=10,source_month='2026-09')
    costly,audit=execute(struct_reference.execute_session,base,extra={'fees_by_asset':{'600000.SH':spread}})
    assert costly.positions[0].quantity==100 and old.positions[0].quantity==200
    fill=audit['fills'][0]
    assert fill['struct_spread']==pytest.approx(fill['notional']*.001)
    assert fill['total_cost']==pytest.approx(fill['commission']+fill['stamp_tax']+fill['slippage']+fill['struct_spread'])
    assert costly.cash>=0 and audit['self_financing_residual']==pytest.approx(0,abs=1e-8)
    assert audit['pnl_residual']==pytest.approx(0,abs=1e-8)


def test_struct_prices_are_lagged_and_missing_estimate_blocks_instead_of_free_trade(setup):
    root,sid,vid=create(setup)
    loc,spec=w.revision(root,sid,vid);panel=setup[3];signals=w.history(loc,panel)
    arm=w.replay(loc,spec,panel,signals,'fixture',start='2026-10-09',end='2026-10-22')
    assert arm['metrics']['struct_spread']>0
    assert all(f['cost_inputs_month']=='2026-09' for f in arm['fills'])
    assert all(f['cost_calibration_id']=='struct_lagged_2021_2023_v1' for f in arm['fills'])
    before=struct_costs.estimates(panel)
    altered={k:v.copy() for k,v in panel.items()}
    altered['close'].loc['2026-10-01':]*=100
    altered['amount'].loc['2026-10-01':]*=10000
    after=struct_costs.estimates(altered)
    np.testing.assert_allclose(before.loc['2026-10-01':],after.loc['2026-10-01':])
    panel['amount'].loc[:'2026-09-30']=np.nan
    with pytest.raises(ValueError,match='缺少上月 STRUCT'):
        w.replay(loc,spec,panel,signals,'fixture',start='2026-10-09',end='2026-10-22')
