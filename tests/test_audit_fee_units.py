"""Order aggregation, notional allocation and conservative evidence boundaries."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from qwb import audit_code, strategy_audit as audit, strategy_imports as imports
from qwb import strategy_audit_ui as view, strategy_audit_store as store, ui
from qwb.audit_diagnostics import fee_analysis

SETTINGS = {'commission_bp': 3., 'minimum_commission': 5., 'max_weight': .5}


def order_bundle(*, basis='order', complete='true', evidence='合同第3项/交割单', order='A1', return_files=False):
    trades = 'date,time,code,side,quantity,price,fee,order_id,fill_id,commission_basis,commission_evidence,order_complete\n'
    trades += f'2024-01-02,09:30,600000.SH,buy,1000,10,3,{order},F1,{basis},{evidence},{complete}\n'
    trades += f'2024-01-02,09:31,600000.SH,buy,1000,10,3,{order},F2,{basis},{evidence},{complete}\n'
    files = [imports.SourceFile('strategy.py', b'# synthetic\nx = 1\n'),
        imports.SourceFile('trades.csv', trades.encode())]
    return files if return_files else imports.normalize(files)


def fees(data):
    return fee_analysis(data, {'daily': [], 'status': 'missing_account'}, SETTINGS)


def test_partial_fills_charge_one_minimum_and_compare_aggregate_not_allocation():
    data = order_bundle()
    # Report allocation can differ from our display allocation and still reconcile.
    data['fills'][0]['fee'], data['fills'][1]['fee'] = 6., 0.
    before = deepcopy(data)
    result = fees(data)
    assert result['commission'] == pytest.approx(6.)
    assert result['modeled_fee'] == pytest.approx(6.)
    assert result['mismatch_count'] == 0
    assert result['confirmed_unit_count'] == 1
    assert result['partial_order_count'] == 1
    assert result['avoided_duplicate_minimum_scenario'] == pytest.approx(4.)
    assert not result['batch_candidates']
    assert sum(r['modeled_commission'] for r in result['fills']) == pytest.approx(6.)
    assert data == before


@pytest.mark.parametrize('basis,complete,evidence', [('unknown','true',''), ('order','false','合同')])
def test_unknown_basis_or_incomplete_order_cannot_create_fee_violation(basis, complete, evidence):
    data = order_bundle(basis=basis, complete=complete, evidence=evidence)
    data['fills'][0]['fee'] = 500
    result = fees(data)
    assert result['mismatch_count'] == 0
    assert result['confirmed_unit_count'] == 0
    assert result['unknown_basis_unit_count'] == 1
    assert result['charge_units'][0]['status'] == 'scenario'
    assert result['scenario_mismatch_count'] > 0


def test_explicit_fill_contract_is_distinct_from_order_contract():
    result = fees(order_bundle(basis='fill'))
    assert result['commission'] == 10
    assert result['confirmed_unit_count'] == 2
    assert result['mismatch_count'] == 2
    assert result['avoided_duplicate_minimum_scenario'] == 0
    # Even with a fill-priced contract, two fills of one order are not two
    # authorized orders that the user can merge.
    assert not result['batch_candidates']


def test_stamp_tax_applies_to_full_sell_notional_on_each_historical_date():
    data = order_bundle()
    for f in data['fills']:
        f['side'] = 'sell'
        f['fee'] = 8.
    result = fees(data)
    assert result['stamp_tax'] == 10.
    assert result['modeled_fee'] == 16.
    assert result['mismatch_count'] == 0
    for f in data['fills']:
        f['date'] = '2023-08-25'
    assert fees(data)['stamp_tax'] == 20.


@pytest.mark.parametrize('bad,match', [
    ({'side': 'sell'}, '不一致'), ({'code': '000001.SZ'}, '不一致'),
    ({'fill_id': 'F1'}, '重复'), ({'order_complete': False}, '不一致'),
])
def test_conflicting_order_identity_and_duplicate_fill_rejected(bad, match):
    data = order_bundle()
    data['fills'][1].update(bad)
    with pytest.raises(ValueError, match=match):
        fees(data)


def test_confirmed_contract_requires_reference_and_order_id():
    with pytest.raises(ValueError, match='计费依据'):
        order_bundle(evidence='')
    with pytest.raises(ValueError, match='委托编号'):
        order_bundle(order='')


def test_fee_identity_cannot_leak_across_dates_or_independent_orders():
    data = order_bundle()
    data['fills'][1]['date'] = '2024-01-03'
    assert fees(data)['commission'] == 10.
    data = order_bundle()
    data['fills'][1]['order_id'] = 'A2'
    assert fees(data)['commission'] == 10.


def test_partial_material_report_and_csv_use_same_order_contract(tmp_path):
    files = order_bundle(return_files=True, evidence='=1+1')
    data = imports.normalize(files)
    result = audit.analyze(data)
    assert result['mode'] == 'partial'
    assert result['modules']['partial_diagnostics']['modeled_fee_scenario'] == 6.
    assert result['modules']['fee_analysis']['mismatch_count'] == 0
    identity = store.save(tmp_path, data, files)
    rid = store.save_report(tmp_path, identity, result)
    client = TestClient(ui.create_app(tmp_path, port=8765))
    response = client.get(f'/strategy-audits/{identity}/reports/{rid}')
    assert response.status_code == 200
    assert '修正记账口径，不是交易节省' in response.text
    exported = client.get(f'/strategy-audits/{identity}/reports/{rid}/fee-units.csv')
    assert exported.status_code == 200
    assert 'A1' in exported.text and 'confirmed' in exported.text
    assert "'=1+1" in exported.text
    assert data['fills'][0]['commission_evidence'] == '=1+1'


def test_big_order_small_partial_fills_do_not_trigger_small_buy_patch():
    data = order_bundle()
    data['adapter'] = 'bigquant_v1'
    data['code']['source'] = 'def handle(context, instrument, cash):\n    if cash > 0:\n        context.order_value(instrument, cash)\n'
    result = audit.analyze(data)
    assert 'small_buy_guard' not in {o['id'] for o in audit_code.inspect(data, result)['options']}


def test_bigquant_optional_chinese_order_columns_are_preserved():
    text = ('日期,时间,证券代码,买/卖,数量,成交价,成交金额,交易费用,委托编号,成交编号,佣金计费单位,计费依据,委托成交完整\n'
            '2024-01-02,09:30,600000.SH,买入,100,10,1000,5,A1,F1,按委托,合同第3项,true\n')
    data = imports.normalize([imports.SourceFile('fills.csv', text.encode())])
    assert data['fills'][0]['order_id'] == 'A1'
    assert fees(data)['confirmed_unit_count'] == 1
