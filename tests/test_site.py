import importlib
import os
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
os.environ["QWB_SITE_AUTOSTART"] = "0"
spec = importlib.util.spec_from_file_location("qwb_share_site", ROOT / "share_site" / "app.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)

SHA = "a" * 40
GOOD = "---\nqwb_version: 0.0.1\ntask: cn_rank_5d\nbase_model: lightgbm\n---\nbody"


class FakeHF:
    def __init__(self, readme=GOOD):
        self.readme_text, self.http = readme, None

    def commit(self, repo):
        return SHA if repo != "no/such" else None

    def readme(self, repo, sha):
        return self.readme_text


def client(tmp_path, hf=None):
    app = site.create_app(tmp_path / "s.db", hf or FakeHF(), oauth={}, secret="x")
    return app, TestClient(app)


def token(c):
    return re.search(r'name="token" value="([^"]+)"', c.get("/").text).group(1)


def test_repo_id_parsing():
    assert site.repo_id("https://huggingface.co/user/model/tree/main") == "user/model"
    assert site.repo_id("user/model") == "user/model"
    assert site.repo_id("../etc/passwd") is None and site.repo_id("javascript:alert(1)") is None


def test_share_requires_login_and_csrf(tmp_path):
    _, c = client(tmp_path)
    assert c.post("/share", data={"repo": "u/m", "token": "x"}).status_code == 401
    c.get("/login/dev")
    assert c.post("/share", data={"repo": "u/m", "token": "bad"}).status_code == 403


def test_share_flow_escapes_author_text_and_keeps_versions(tmp_path):
    app, c = client(tmp_path)
    c.get("/login/dev")
    r = c.post("/share", data={"repo": "u/m", "author_text": "<script>x()</script>",
                               "token": token(c)}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert "<script>x()</script>" not in page and "&lt;script&gt;" in page
    assert "lightgbm" in page and "未经平台验证" in page

    sid = int(r.headers["location"].rsplit("/", 1)[1])
    c.post(f"/m/{sid}/deprecate", data={"token": token(c)})
    assert "已弃用" in c.get(f"/m/{sid}").text
    assert app.state.db.execute("select count(*) from shares").fetchone()[0] == 1  # not deleted


def test_source_gone_is_shown(tmp_path):
    hf = FakeHF()
    app, c = client(tmp_path, hf)
    c.get("/login/dev")
    r = c.post("/share", data={"repo": "u/m", "token": token(c)}, follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    hf.readme_text = None
    assert app.state.recheck(sid) == "gone"
    assert "源文件已不可用" in c.get("/").text


def test_flags_and_bad_cards(tmp_path):
    _, c = client(tmp_path, FakeHF("---\ntask: x\n---\n稳赚，加微信进付费群"))
    c.get("/login/dev")
    r = c.post("/share", data={"repo": "u/m", "token": token(c)}, follow_redirects=True)
    assert "内容检查提示" in r.text and "不是 TemperQ 生成的卡片" in r.text

    (tmp_path / "b").mkdir()
    _, c2 = client(tmp_path / "b", FakeHF("no front matter"))
    c2.get("/login/dev")
    assert c2.post("/share", data={"repo": "u/m", "token": token(c2)}).status_code == 400
    assert c2.post("/share", data={"repo": "no/such", "token": token(c2)}).status_code == 400


def test_dev_login_disabled_when_oauth_configured(tmp_path):
    app = site.create_app(tmp_path / "s.db", FakeHF(), secret="x",
                          oauth={"client_id": "a", "client_secret": "b", "redirect_uri": "c"})
    c = TestClient(app)
    assert c.get("/login/dev").status_code == 404
    assert c.get("/login", follow_redirects=False).headers["location"].startswith(
        "https://huggingface.co/oauth/authorize")


def test_site_api_shares_session_and_preserves_untrusted_text(tmp_path):
    _, c = client(tmp_path)
    anonymous = c.get("/api/site").json()
    assert anonymous["user"] is None and anonymous["models"] == []
    assert anonymous["csrf"] and anonymous["dev"]
    c.get("/login/dev")
    payload = c.get("/api/site").json()
    r = c.post("/share", data={"repo": "u/m", "author_text": "<img onerror=alert(1)>",
                               "token": payload["csrf"]}, follow_redirects=False)
    assert r.status_code == 303
    listing = c.get("/api/site").json()
    assert listing["user"] == "local-dev"
    assert listing["models"][0]["fields"]["base_model"] == "lightgbm"
    assert listing["models"][0]["author_text"] == "<img onerror=alert(1)>"
    assert listing["models"][0]["sha"] == SHA
    c.get("/logout")
    assert c.get("/api/site").json()["user"] is None


def test_reference_and_downloads_are_fixed_public_artifacts(tmp_path):
    _, c = client(tmp_path)
    report = c.get("/api/reference").json()["report"]
    assert "0.1104" in report and "不显著" in report
    download = c.get("/downloads/report")
    assert download.status_code == 200 and download.text == report
    assert "attachment" in download.headers["content-disposition"]
    recipe = c.get("/downloads/recipe")
    assert recipe.status_code == 200 and "name: lightgbm" in recipe.text
    assert c.get("/downloads/config.local.yaml").status_code == 404


def test_author_research_is_validated_and_escaped(tmp_path):
    _, c = client(tmp_path)
    c.get("/login/dev")
    data = {"repo": "u/research", "token": token(c), "rank_ic": "0.12",
            "net_excess_ann": "-2.5", "test_period": "2024–2025",
            "recipe_text": "<script>alert(1)</script>"}
    r = c.post("/share", data=data, follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert "+0.1200" in page and "-2.50%" in page and "作者自报" in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    model = c.get("/api/site").json()["models"][0]
    assert model["research"]["net_excess_ann"] == -2.5
    for value in ("NaN", "Infinity", "1.1", "not-a-number"):
        assert c.post("/share", data=data | {"rank_ic": value}).status_code == 422
    assert c.post("/share", data=data | {"test_period": ""}).status_code == 422


def test_dev_login_refused_behind_a_proxy(tmp_path):
    _, c = client(tmp_path)
    r = c.get("/login/dev", headers={"X-Forwarded-For": "203.0.113.9"}, follow_redirects=False)
    assert r.status_code == 404
    r = c.get("/login/dev", headers={"Forwarded": "for=203.0.113.9"}, follow_redirects=False)
    assert r.status_code == 404


def test_env_startup_requires_oauth_or_explicit_dev(tmp_path, monkeypatch):
    for k in ("HF_OAUTH_CLIENT_ID", "HF_OAUTH_CLIENT_SECRET", "HF_OAUTH_REDIRECT_URI",
              "QWB_SITE_DEV", "QWB_SITE_SECRET"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="HF OAuth"):
        site.create_app(tmp_path / "a.db", FakeHF())
    monkeypatch.setenv("QWB_SITE_DEV", "1")
    site.create_app(tmp_path / "b.db", FakeHF())                       # local testing: allowed
    monkeypatch.delenv("QWB_SITE_DEV")
    monkeypatch.setenv("HF_OAUTH_CLIENT_ID", "a")
    monkeypatch.setenv("HF_OAUTH_CLIENT_SECRET", "b")
    monkeypatch.setenv("HF_OAUTH_REDIRECT_URI", "https://x/oauth/callback")
    with pytest.raises(RuntimeError, match="QWB_SITE_SECRET"):
        site.create_app(tmp_path / "c.db", FakeHF())
    monkeypatch.setenv("QWB_SITE_SECRET", "s" * 40)
    app = site.create_app(tmp_path / "d.db", FakeHF())
    c = TestClient(app, base_url="https://testserver")
    c.get("/api/site")
    assert any(ck.secure for ck in c.cookies.jar)                      # HTTPS-only session cookie


def test_rate_limit_window_and_keys():
    now = [0.0]
    rl = site.RateLimit({"short": (3, 60)}, clock=lambda: now[0])
    assert [rl.hit(["ip:a"]) for _ in range(3)] == [0, 0, 0]
    assert rl.hit(["ip:a"]) > 0                      # 4th within a minute: wait
    assert rl.hit(["ip:b"]) == 0                     # another client is not affected
    now[0] = 61
    assert rl.hit(["ip:a"]) == 0                     # window passed


def test_posts_are_rate_limited(tmp_path):
    _, c = client(tmp_path)
    c.get("/login/dev")
    codes = [c.post("/share", data={"repo": "u/m", "token": "bad"}).status_code for _ in range(12)]
    assert codes[:10] == [403] * 10 and codes[10] == 429
    assert c.get("/").status_code == 200             # reads are never limited


def test_db_backup_keeps_newest_and_is_consistent(tmp_path):
    import importlib.util
    import sqlite3
    spec = importlib.util.spec_from_file_location("bk", ROOT / "scripts" / "backup_site_db.py")
    bk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bk)
    app, c = client(tmp_path)
    c.get("/login/dev")
    tok = re.search(r'name="token" value="([^"]+)"', c.get("/").text).group(1)
    c.post("/share", data={"repo": "u/m", "token": tok})
    out = tmp_path / "bk"
    for i in range(3):
        (out / f"s-2000010{i}T000000Z.db").parent.mkdir(exist_ok=True)
        (out / f"s-2000010{i}T000000Z.db").write_bytes(b"old")
    dest = bk.backup(tmp_path / "s.db", out, keep=2)
    assert sorted(p.name for p in out.glob("s-*.db"))[-1] == dest.name
    assert len(list(out.glob("s-*.db"))) == 2
    assert sqlite3.connect(dest).execute("select count(*) from shares").fetchone()[0] == 1


def test_site_database_env_creates_parent_and_healthcheck(tmp_path, monkeypatch):
    database = tmp_path / "persistent" / "site.db"
    monkeypatch.setenv("QWB_SITE_DB", str(database))
    app = site.create_app(hf=FakeHF(), oauth={}, secret="test")
    assert database.is_file()
    c = TestClient(app)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/api/site").json()["user"] is None
    app.state.db.close()


def test_explicit_database_overrides_server_env(tmp_path, monkeypatch):
    monkeypatch.setenv("QWB_SITE_DB", str(tmp_path / "server.db"))
    app = site.create_app(tmp_path / "test.db", FakeHF(), oauth={}, secret="test")
    assert (tmp_path / "test.db").is_file()
    assert not (tmp_path / "server.db").exists()
    app.state.db.close()


def test_temperq_environment_overrides_legacy_without_exposing_dev_login(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMPERQ_SITE_DB", str(tmp_path / "temperq" / "site.db"))
    monkeypatch.setenv("QWB_SITE_DB", str(tmp_path / "legacy.db"))
    app = site.create_app(hf=FakeHF(), oauth={}, secret="test")
    assert (tmp_path / "temperq" / "site.db").is_file()
    assert not (tmp_path / "legacy.db").exists()
    client = TestClient(app)
    assert client.get("/login/dev", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 404
    app.state.db.close()


def test_hf_readme_stops_reading_oversized_stream():
    import httpx

    seen = []
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for i in range(100):
                seen.append(i)
                yield b"x" * 65536
    hf = site.HFClient()
    hf.http.close()
    hf.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Stream())))
    try:
        assert hf.readme("u/m", SHA) is None
        assert len(seen) == 4
    finally:
        hf.http.close()


def test_share_vendor_timeout_is_retryable_and_does_not_store_a_model(tmp_path):
    import httpx

    class OfflineHF(FakeHF):
        def commit(self, repo):
            raise httpx.ConnectTimeout("private connection details")
    _, c = client(tmp_path, OfflineHF())
    c.get("/login/dev")
    response = c.post("/share", data={"repo": "u/m", "token": token(c)})
    assert response.status_code == 503
    assert "private connection" not in response.text
    assert c.get("/api/site").json()["models"] == []


def test_backups_never_overwrite_even_with_identical_clock(tmp_path, monkeypatch):
    import sqlite3
    from datetime import datetime, timezone

    spec = importlib.util.spec_from_file_location("backup_collision", ROOT / "scripts" / "backup_site_db.py")
    backup_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup_module)
    fixed = datetime(2026, 10, 23, tzinfo=timezone.utc)
    monkeypatch.setattr(backup_module, "datetime", type("Clock", (), {"now": staticmethod(lambda tz: fixed)}))
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as con:
        con.execute("create table records(value integer)")
        con.execute("insert into records values (1)")
    first = backup_module.backup(source, tmp_path / "copies")
    with sqlite3.connect(source) as con:
        con.execute("insert into records values (2)")
    second = backup_module.backup(source, tmp_path / "copies")
    assert first != second and first.is_file() and second.is_file()
    for path, count in [(first, 1), (second, 2)]:
        with sqlite3.connect(path) as restored:
            assert restored.execute("pragma integrity_check").fetchone()[0] == "ok"
            assert restored.execute("select count(*) from records").fetchone()[0] == count
