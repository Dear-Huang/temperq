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
    assert "内容检查提示" in r.text and "不是 qwb 生成的卡片" in r.text

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
