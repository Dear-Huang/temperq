"""Import -> independent timing replay -> result, without touching paper accounts."""

from io import StringIO

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qwb import experiments as ex, strategy_workspace as ws, ui
from test_strategy_workspace import setup


@pytest.fixture
def client(setup, monkeypatch):
    workdir, run, feed, panel = setup
    sid, vid = ws.create(workdir, run, feed, name='原账户', cash=100000, preset='direct',
                         overrides={'n_hold': 1, 'hold_until_rank': 1, 'max_weight': 1., 'exposure_cap': .5},
                         cost_model='commission_only')
    monkeypatch.setattr(ui, 'datasets', lambda p: {'daily': {'path': feed}})
    app = ui.create_app(workdir, runner=lambda fn: fn())
    return TestClient(app), app.state.token, f'/strategies/{sid}/{vid}', workdir


def submission(client):
    http, token, base, workdir = client
    template = http.get(base+'/timing-template.csv')
    assert template.status_code == 200
    frame = pd.read_csv(StringIO(template.text.lstrip('\ufeff')), dtype={'code': str})
    assert frame.available_at.isna().all()  # Do not fabricate historical generation times.
    frame['available_at'] = frame.decision_time
    frame['data_asof'] = (pd.to_datetime(frame.decision_time, utc=True)-pd.Timedelta(minutes=30)).astype(str)
    days = sorted(frame.decision_time.unique())
    codes = list(frame.code.unique())
    frame['prediction'] = [(.02 if code == (codes[0] if at < days[2] else codes[1]) else 0.)
                           for at, code in zip(frame.decision_time, frame.code)]
    data = {'token': token, 'start': '2026-10-09', 'end': '2026-10-22', 'version': 'user-v2',
            'score_kind': 'expected_return', 'horizon_sessions': '5', 'evaluation_horizon': '5',
            'period_sessions': '5', 'allow_between': 'yes', 'extra_turnover_cap': '2',
            'max_extra_per_week': '1', 'min_sessions_between_extra': '2', 'extra_direction': 'both',
            'cost_multiplier': '1.5', 'minimum_surplus_bp': '5',
            'calibration_id': 'user-calibration-v1', 'calibration_end_at': '2026-10-01T15:30:00+08:00'}
    return data, frame


def test_rules_entry_and_complete_import_comparison_result_preserve_frozen_spec(client):
    http, token, base, workdir = client
    sid, vid = base.split('/')[-2:]
    root, spec = ws.revision(workdir, sid, vid)
    before = (root/'spec.json').read_bytes()
    assert '调仓时机试验' in http.get(base).text
    page = http.get(base+'/timing')
    assert page.status_code == 200 and '高级设置' in page.text
    assert '当前纸面账户继续使用原规则' in page.text
    data, frame = submission(client)
    response = http.post(base+'/timing', data=data,
                         files={'signals': ('signals.csv', frame.to_csv(index=False).encode(), 'text/csv')},
                         follow_redirects=False)
    assert response.status_code == 303, response.text
    result = http.get(response.headers['location'])
    assert result.status_code == 200 and '尚未证明收益优势' in result.text
    assert 'EXTRA_AUTHORIZED' in result.text
    output = next(root.glob('timing-experiments/*/result.json'))
    record = ex.read(output)
    assert record['status'] == 'SUCCEEDED' and record['evidence'] == 'INSUFFICIENT'
    assert (output.parent/'signals.csv').is_file()
    assert set(record['metrics']) == {'periodic', 'selected'}
    assert (root/'spec.json').read_bytes() == before
    assert not (root/'runtime.json').exists() and not (root/'ledger.sqlite3').exists()
    ws.revision(workdir, sid, vid)  # Frozen implementation check still passes.


def test_import_rejects_late_generation_and_keeps_failed_attempt(client):
    http, _, base, workdir = client
    data, frame = submission(client)
    frame.loc[0, 'available_at'] = '2026-10-09T16:00:00+08:00'
    response = http.post(base+'/timing', data=data,
                         files={'signals': ('late.csv', frame.to_csv(index=False).encode())})
    assert response.status_code == 400
    sid, vid = base.split('/')[-2:]
    root, _ = ws.revision(workdir, sid, vid)
    result = ex.read(next(root.glob('timing-experiments/*/result.json')))
    assert result['status'] == 'FAILED'
    assert not (root/'runtime.json').exists()


def test_ranking_cannot_trigger_return_gate_and_bad_token_is_rejected(client):
    http, _, base, _ = client
    data, frame = submission(client)
    raw = frame.to_csv(index=False).encode()
    denied = http.post(base+'/timing', data=dict(data, token='wrong'), files={'signals': ('s.csv', raw)})
    assert denied.status_code == 403
    data['score_kind'] = 'ranking'
    result = http.post(base+'/timing', data=data, files={'signals': ('s.csv', raw)})
    assert result.status_code == 200 and 'UNCALIBRATED_SIGNAL' in result.text
    assert 'EXTRA_AUTHORIZED' not in result.text
    assert http.get(base+'/timing/not-an-id').status_code == 404
