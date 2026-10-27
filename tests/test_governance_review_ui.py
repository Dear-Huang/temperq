import csv
import io

from fastapi.testclient import TestClient
import pytest

from qwb import experiments as ex, governance_review_ui as view, review_store, ui


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "hardware", lambda: {})
    app = ui.create_app(tmp_path, runner=lambda fn: None)
    return app, TestClient(app), tmp_path


def test_new_home_is_governance_and_legacy_routes_remain(client):
    _, c, _ = client
    assert c.get("/", follow_redirects=False).headers["location"] == "/strategy-governance"
    assert "治理工作台" in c.get("/review").text
    assert "我的策略" in c.get("/start").text
    assert "计算诊断与调整方案" in c.get("/review/new").text
    assert "合成示例，价格与持仓均非真实" in c.get("/review/new?example=1").text


def create(client, value=None):
    app, c, _ = client
    response = c.post("/api/governance-reviews", json=value or view.sample_input(), headers={"x-temperq-token": app.state.token})
    assert response.status_code == 201, response.text
    return response.json()


def test_api_security_validation_and_bounded_payload(client):
    app, c, _ = client
    endpoint = "/api/governance-reviews"
    assert c.post(endpoint, json=view.sample_input()).status_code == 403
    assert c.post(endpoint, json=view.sample_input(), headers={"x-temperq-token": app.state.token, "host": "evil.example"}).status_code == 403
    assert c.post(endpoint, content=b"x" * (view.MAX_BYTES + 1), headers={"x-temperq-token": app.state.token}).status_code == 413
    invalid = view.sample_input(); invalid["cash"] = -1
    assert c.post(endpoint, json=invalid, headers={"x-temperq-token": app.state.token}).status_code == 422


def test_form_end_to_end_evidence_confirmation_and_no_account_mutation(client):
    app, c, workdir = client
    fields = view.form_values(view.sample_input())
    fields.update(token=app.state.token, account_clear="yes")
    response = c.post("/review", data=fields, follow_redirects=False)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    page = c.get(location)
    assert page.status_code == 200
    assert '/static/strategy-audit.css' in page.text
    assert "推荐调整" in page.text and "逐环节诊断" in page.text and "方案对照" in page.text
    assert '目标受阻后，还能调整多少' in page.text and '股票偏离下界' in page.text
    assert '只做朝向原目标的调整' in page.text
    evidence = c.get(location + "/evidence.json").json()
    result = evidence["result"]
    assert result["recommended_id"]
    assert not (workdir / "runs/strategies").exists()
    assert not (workdir / "runs/paper").exists()
    fields = dict(token=app.state.token, candidate_id=result["recommended_id"], result_digest=result["result_digest"])
    assert c.post(location + "/select", data=dict(fields, token="bad")).status_code == 403
    assert c.post(location + "/select", data=dict(fields, result_digest="stale")).status_code == 409
    selected = c.post(location + "/select", data=fields, follow_redirects=False)
    assert selected.status_code == 303
    destination = selected.headers["location"]
    selection = destination.split("selection=")[1]
    assert "方案已保存" in c.get(destination).text
    plan = c.get(location + "/plans/" + selection + ".csv")
    assert plan.status_code == 200 and "proposed_quantity" in plan.text
    assert "修改输入再比较" in c.get(location).text
    assert not (workdir / "runs/strategies").exists()


def test_tolerance_export_and_explicit_scenario_selection(client):
    app, c, workdir = client
    created = create(client)
    url, result = created['url'], created['result']
    page = c.get(url)
    assert '接受多少偏离，能少付多少费用' in page.text
    assert 'aria-label="账户方案章节"' in page.text
    export = c.get(url + '/comparison.csv')
    assert export.status_code == 200
    rows = list(csv.DictReader(io.StringIO(export.text.lstrip('\ufeff'))))
    assert len(rows) == 3
    assert rows[0]['additional_tolerance'] == '0.0'
    candidate = result['tolerance_comparison']['rows'][0]['candidate_id']
    selected = c.post(url + '/select', data=dict(token=app.state.token, candidate_id=candidate,
                      result_digest=result['result_digest']), follow_redirects=False)
    assert selected.status_code == 303
    assert not (workdir / 'runs/paper').exists()
    # Old reports keep their original digest and do not gain fabricated scenarios.
    directory = review_store.root(workdir, created['id'])
    result.pop('tolerance_comparison')
    result['result_digest'] = ex.object_digest({k: v for k, v in result.items() if k != 'result_digest'})
    record = ex.read(directory / 'record.json'); record['result_digest'] = result['result_digest']
    ex.write(directory / 'result.json', result); ex.write(directory / 'record.json', record)
    assert c.get(url).status_code == 200
    assert '接受多少偏离，能少付多少费用' not in c.get(url).text
    assert c.get(url + '/comparison.csv').status_code == 404


def test_import_files_and_validation_preserve_form(client):
    app, c, _ = client
    values = view.form_values(view.sample_input()); values.update(token=app.state.token, account_clear="yes")
    signals = values.pop("signals")
    r = c.post("/review", data=values, files={"signals_file": ("scores.csv", signals.encode(), "text/csv")}, follow_redirects=False)
    assert r.status_code == 303, r.text
    values["cash"] = "-1"
    bad = c.post("/review", data=values, files={"signals_file": ("scores.csv", signals.encode(), "text/csv")})
    assert bad.status_code == 400
    assert "合成示例 · 换仓、锁仓与禁买" in bad.text
    assert "600000.SH,0.22" in bad.text


def test_report_rejects_tampered_input_and_invalid_ids(client):
    _, c, workdir = client
    result = create(client)
    path = review_store.root(workdir, result["id"]) / "input.json"
    value = ex.read(path); value["cash"] += 1; ex.write(path, value)
    assert c.get(result["url"]).status_code == 404
    assert c.get("/review/not-an-id").status_code == 404
    assert c.get("/review/templates/unknown.csv").status_code == 404


def test_user_name_is_escaped_and_unknown_fields_not_silently_ignored(client):
    app, c, _ = client
    value = view.sample_input(); value["name"] = "<script>bad()</script>"
    result = create(client, value)
    assert "<script>bad()</script>" not in c.get(result["url"]).text
    value["orders"] = [{"action": "buy"}]
    r = c.post("/api/governance-reviews", json=value, headers={"x-temperq-token": app.state.token})
    assert r.status_code == 422


def test_infeasible_review_cannot_be_selected(client):
    app, c, _ = client
    value = view.sample_input()
    for holding in value["holdings"]:
        holding["sellable_quantity"] = 0
    value["rules"]["max_weight"] = .1
    result = create(client, value)
    assert result["result"]["recommended_id"] is None
    response = c.post(result["url"] + "/select", data=dict(token=app.state.token, candidate_id="hold", result_digest=result["result"]["result_digest"]))
    assert response.status_code == 409


def test_repeat_confirmation_appends_instead_of_overwriting(client):
    app, c, workdir = client
    result = create(client)
    data = dict(token=app.state.token, candidate_id=result["result"]["recommended_id"], result_digest=result["result"]["result_digest"])
    for _ in range(2):
        assert c.post(result["url"] + "/select", data=data, follow_redirects=False).status_code == 303
    assert len(list((review_store.root(workdir, result["id"]) / "selections").glob("*.json"))) == 2


def test_repeated_concurrent_reviews_preserve_independent_results(client):
    from concurrent.futures import ThreadPoolExecutor

    app, _, workdir = client

    def submit(index):
        value = view.sample_input()
        value["name"] = f"Concurrent synthetic account {index}"
        with TestClient(app) as c:
            response = c.post(
                "/api/governance-reviews", json=value,
                headers={"x-temperq-token": app.state.token},
            )
            assert response.status_code == 201, response.text
            result = response.json()
            evidence = c.get(result["url"] + "/evidence.json")
            assert evidence.status_code == 200
            assert evidence.json()["input"]["name"] == value["name"]
            chosen = next(candidate for candidate in result["result"]["candidates"]
                          if candidate["id"] == result["result"]["recommended_id"])
            assert chosen["feasible"] and not chosen["violations"]
            return result["id"], chosen["rows"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(submit, range(9)))
    assert len({identifier for identifier, _ in results}) == 9
    assert all(rows == results[0][1] for _, rows in results)
    assert not (workdir / "runs/paper").exists()


def test_sample_produces_trades_and_respects_locks_and_blocks(client):
    result = create(client)["result"]
    chosen = next(c for c in result["candidates"] if c["id"] == result["recommended_id"])
    rows = {r["code"]: r for r in chosen["rows"]}
    assert chosen["feasible"] and chosen["order_count"] > 0
    assert rows["000001.SZ"]["delta"] >= 0          # locked: cannot be sold
    assert rows["000858.SZ"]["delta"] <= 0          # buy-blocked: cannot be bought
    assert rows["601318.SH"]["delta"] > 0           # new name enters
    assert rows["002415.SZ"]["delta"] < 0           # dropped name is reduced


def test_datetime_local_inputs_round_trip_as_beijing_time(client):
    app, c, workdir = client
    values = view.form_values(view.sample_input())
    assert values["observed_at"] == "2026-10-26T15:30"
    values.update(token=app.state.token, account_clear="yes")
    response = c.post("/review", data=values, follow_redirects=False)
    assert response.status_code == 303, response.text
    review_id = response.headers["location"].rsplit("/", 1)[1]
    _, original, _ = review_store.read(workdir, review_id)
    assert original["observed_at"].endswith("+08:00") and original["observed_at"].startswith("2026-10-26T15:30")


def test_single_primary_action_and_duplicate_plans_marked(client):
    _, c, _ = client
    page = c.get(create(client)["url"]).text
    assert page.count('class="primary" type="submit">确认并保存推荐方案') == 1
    assert '确认并保存方案</button>' not in page
    history = c.get("/review").text
    assert "笔调整" in history and "预计费用" in history
