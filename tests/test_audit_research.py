from copy import deepcopy

import pytest

from qwb import strategy_audit as audit, strategy_audit_ui as view, strategy_imports as imp
from qwb.struct_costs import tick_half_spread_bp


def bundle(cash=10000):
    return imp.normalize(view.sample_files(), opening_cash=cash)


def partial_bundle():
    data = bundle()
    data['actions'] = []
    data['fills'] = []
    for day, side, qty, price in [('2024-01-02', 'buy', 200, 4.),
                                 ('2024-01-03', 'sell', 50, 5.), ('2024-01-05', 'sell', 50, 6.)]:
        data['fills'].append({'date': day, 'time': '10:00', 'code': '600000.SH', 'side': side,
            'quantity': qty, 'price': price, 'notional': qty*price, 'fee': 5.,
            'ref': {'source': 'fills', 'row': len(data['fills'])+2}})
    data['positions'], data['snapshots'] = [], []
    for day, qty, price, cash in [('2024-01-02', 200, 4., 9195.),
        ('2024-01-03', 150, 5., 9440.), ('2024-01-05', 100, 7., 9735.)]:
        data['positions'].append({'date': day, 'code': '600000.SH', 'quantity': qty,
            'price': price, 'market_value': qty*price, 'ref': {}})
        data['snapshots'].append({'date': day, 'cash': cash, 'equity': cash+qty*price,
            'market_value': qty*price, 'ref': {}})
    return data


def test_tick_component_formula_and_baseline_alignment():
    assert tick_half_spread_bp(5.) == 10.
    assert tick_half_spread_bp(10.) == 5.
    for bad in (0, -1, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            tick_half_spread_bp(bad)
    result = audit.analyze(bundle())
    tick = result['modules']['tick_analysis']
    assert tick['all_exported_fills']['tick_scenario_cost'] == 1.5
    assert tick['accounted_intervals']['net_after_tick_scenario'] == pytest.approx(182.4)
    assert tick['auction_boundary_fills'] == 3
    assert tick['measured_execution_cost'] is None
    assert '不是实盘成本下限' in tick['meaning']
    first_close = audit.analyze(bundle(None))['modules']['tick_analysis']
    assert first_close['accounted_intervals']['fills'] == 1
    assert first_close['accounted_intervals']['tick_scenario_cost'] == .5
    assert first_close['accounted_intervals']['net_after_tick_scenario'] == pytest.approx(193.4)
    assert sum(r['tick_scenario_cost'] for r in tick['price_bands']) == 1.5


def test_fifo_partial_fees_and_right_censored_lot_close_net_bridge():
    result = audit.analyze(partial_bundle())
    assert result['modules']['accounting']['status'] == 'verified'
    h = result['modules']['holding_analysis']
    assert [r['quantity'] for r in h['closed']] == [50, 50]
    assert [r['entry_fee'] for r in h['closed']] == [1.25, 1.25]
    assert [r['exit_fee'] for r in h['closed']] == [5., 5.]
    assert h['closed_summary']['gross_pnl'] == 150
    assert h['closed_summary']['fees'] == 12.5
    assert h['closed_summary']['net_pnl'] == 137.5
    assert h['closed_summary']['tick_scenario_cost'] == 1.
    assert len(h['open']) == 1
    opened = h['open'][0]
    assert opened['right_censored'] and opened['exit_date'] is None
    assert opened['exit_fee'] == 0 and opened['entry_fee'] == 2.5
    assert opened['net_pnl'] == 297.5
    assert opened['tick_scenario_cost'] == .5  # no invented future sell cost
    assert h['coverage']['net_bridge_gap'] == 0
    assert h['quantity_weighted_calendar_days'] == 2
    assert h['quantity_weighted_snapshot_intervals'] == 1.5
    assert [r['quantity'] for r in h['closed_age_bands']] == [50, 50, 0, 0]
    low = h['entry_price_bands'][0]
    assert low['closed']['net_pnl'] + low['open']['net_pnl'] == 435


def test_unknown_opening_never_receives_fabricated_buy_date_cost_or_age():
    data = partial_bundle()
    data['opening'] = {'kind': 'first_close', 'cash': None}
    h = audit.analyze(data)['modules']['holding_analysis']
    assert not h['closed'] and not h['open']
    assert h['quantity_weighted_calendar_days'] is None
    assert h['coverage']['unknown_opening_sale_quantity'] == 100
    assert h['coverage']['unknown_opening_remaining'][0]['quantity'] == 100
    assert h['coverage']['net_bridge_comparable'] is False
    assert h['coverage']['baseline_excluded_fill_count'] == 1


def test_company_actions_and_invalid_inventory_are_explicitly_excluded_not_spurious_losses():
    data = partial_bundle()
    data['actions'] = [{'date': '2024-01-03', 'code': '600000.SH', 'quantity_change': 0, 'cash': 10., 'ref': {}}]
    h = audit.analyze(data)['modules']['holding_analysis']
    assert h['coverage']['excluded_action_or_invalid_codes'] == 1
    assert h['coverage']['excluded_fill_count'] == 3
    assert not h['closed'] and not h['open']
    data = partial_bundle()
    data['positions'][1]['quantity'] += 1
    h = audit.analyze(data)['modules']['holding_analysis']
    assert h['status'] == 'needs_reconciliation'
    assert h['coverage']['excluded_fill_count'] == 3


def test_same_day_new_buys_cannot_hide_t1_inventory_gaps():
    data = partial_bundle()
    data['fills'][1]['date'] = data['fills'][0]['date']
    h = audit.analyze(data)['modules']['holding_analysis']
    assert h['coverage']['excluded_fill_count'] == 3
    assert not h['closed']


def quote_file(text='2024-01-02,600000.SH,14,100,daily_quote_mean\n'):
    return imp.SourceFile('quotes.csv', ('date,code,quoted_spread_bp,sample_count,measurement_scope\n'+text).encode())


def test_optional_sixth_material_daily_quote_comparison_and_coverage():
    files = view.sample_files() + [quote_file(), imp.SourceFile('industry.csv', '行业名称,组合收益率,基准收益率\n行业A,1,2\n'.encode())]
    data = imp.normalize(files, opening_cash=10000)
    calibration = audit.analyze(data)['modules']['tick_analysis']['calibration']
    assert len(data['sources']) == 6
    assert calibration['matched_fills'] == 1
    assert calibration['notional_coverage'] == pytest.approx(1000/4100)
    assert calibration['weighted_quoted_half_spread_bp'] == 7.
    assert calibration['weighted_tick_half_spread_bp'] == 5.
    assert calibration['matches'][0]['quote_ref']['source'] == 'quote_observations'
    assert 'not_trade_time_execution' in calibration['scope']
    with pytest.raises(ValueError, match='最多上传 10 个'):
        imp.normalize(files + [quote_file()] * 5)


@pytest.mark.parametrize('row', [
    '2024-01-02,600000.SH,-1,100,daily_quote_mean\n',
    '2024-01-02,600000.SH,10001,100,daily_quote_mean\n',
    '2024-01-02,600000.SH,1,0,daily_quote_mean\n',
    '2024-01-02,600000.SH,1,1.5,daily_quote_mean\n',
    '2024-01-02,600000.SH,1,10,auction_execution\n',
    '2024-01-02,600000.SH,nan,10,daily_quote_mean\n'])
def test_quote_input_units_counts_scope_rejected(row):
    with pytest.raises(ValueError):
        imp.normalize(view.sample_files()+[quote_file(row)])


def test_quote_no_matching_day_and_subtick_values_do_not_get_clipped():
    data = imp.normalize(view.sample_files()+[quote_file('2023-01-02,600000.SH,1,100,daily_quote_mean\n')])
    t = audit.analyze(data)['modules']['tick_analysis']
    assert t['calibration_status'] == 'no_matching_observations'
    assert t['calibration']['weighted_quoted_half_spread_bp'] is None
    data = imp.normalize(view.sample_files()+[quote_file('2024-01-02,600000.SH,1,100,daily_quote_mean\n')])
    t = audit.analyze(data)['modules']['tick_analysis']
    assert t['calibration']['below_tick_proxy_fills'] == 1
    assert t['calibration']['weighted_quoted_half_spread_bp'] == .5
    with pytest.raises(ValueError, match='重复'):
        imp.normalize(view.sample_files()+[quote_file('2024-01-02,600000.SH,1,100,daily_quote_mean\n'*2)])


def test_price_boundaries_and_account_draft_never_claim_cost_coverage():
    data = partial_bundle()
    r = audit.analyze(data)
    t = r['modules']['tick_analysis']
    assert [p['fills'] for p in t['price_bands']] == [1, 2, 0, 0, 0]  # last band: ETF/fund/bond
    assert t['accounted_intervals']['profit_covers_scenario'] is True
    data['snapshots'][-1]['cash'] += 1
    t = audit.analyze(data)['modules']['tick_analysis']
    assert t['accounted_intervals']['profit_covers_scenario'] is None


def test_old_reports_do_not_acquire_new_calculations_and_new_reports_are_deterministic():
    result = audit.analyze(bundle())
    assert result == audit.analyze(bundle())
    html = view.research_section(result, '/test')
    assert 'hold_days' in html and '右删失' in html
    assert '信号年龄' in html and '不是实盘成本下限' in html
    old = deepcopy(result)
    old['modules'].pop('tick_analysis'); old['modules'].pop('holding_analysis')
    assert view.research_section(old, '/test') == ''


def test_l2_extractor_reads_only_fill_dates_tracks_schema_gaps_and_never_overwrites(tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    import pyarrow as pa
    import pyarrow.parquet as pq

    exports = tmp_path/'exports'; exports.mkdir()
    for f in view.sample_files():
        (exports/f.name).write_bytes(f.data)
    warehouse = tmp_path/'warehouse'; warehouse.mkdir()
    day1 = warehouse/'date=20240102'; day1.mkdir()
    pq.write_table(pa.Table.from_pylist([
        {'trade_date': '2024-01-02', 'wind_code': '600000.SH', '相对价差_均': .0014, '快照数': 100},
        {'trade_date': '2024-01-02', 'wind_code': '000001.SZ', '相对价差_均': .001, '快照数': 2}]), day1/'part.parquet')
    day2 = warehouse/'date=20240103'; day2.mkdir()
    pq.write_table(pa.table({'unrelated': [1]}), day2/'part.parquet')
    sealed = warehouse/'date=20260101'; sealed.mkdir()
    (sealed/'part.parquet').write_bytes(b'must never be opened as parquet')
    output = tmp_path/'new-output'
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, '-X', 'utf8', str(root/'scripts/validate_audit_tick_quotes.py'), '--exports', str(exports),
        '--l2-daily', str(warehouse), '--output', str(output), '--opening-cash', '10000']
    env = {**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'QWB_SKIP_PRELOAD': '1'}
    first = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', env=env)
    assert first.returncode == 0, first.stderr
    summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
    assert summary['calibration']['matched_fills'] == 1
    assert summary['unsupported_schema_dates'] == ['2024-01-03']
    assert summary['quality_rejected_security_dates'] == 1
    quote = imp.SourceFile('quotes.csv', (output/'quoted_spreads.csv').read_bytes())
    assert len(imp.normalize(view.sample_files()+[quote])['quote_observations']) == 1
    original = (output/'summary.json').read_bytes()
    again = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', env=env)
    assert again.returncode != 0 and 'new directory' in again.stderr
    assert (output/'summary.json').read_bytes() == original
