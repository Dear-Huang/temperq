"""Training history, independent evaluation, paired comparison and evidence export."""

from __future__ import annotations

import html
import uuid
from pathlib import Path

import yaml
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from . import evidence_export, experiments

STATE_LABELS = {"running": "运行中", "succeeded": "完成", "failed": "失败", "cancelled": "已取消"}


def esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


def register(app, workdir: Path, *, page, guard, submit, token: str, markdown):
    def locate(run_id):
        try:
            root = experiments.run_dir(workdir, run_id)
            return root, experiments.read(root / "run.json")
        except (OSError, ValueError):
            raise HTTPException(404, "训练记录不存在") from None

    def evaluation(evaluation_id):
        try:
            return experiments.evaluation_dir(workdir, evaluation_id)
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from None

    def protocols():
        out = {}
        from .bench.contracts import validate_protocol
        for path in sorted((workdir / "bench" / "protocols").glob("*.yaml")):
            try:
                value = yaml.safe_load(path.read_text(encoding="utf-8"))
                validate_protocol(value)
                out[path.name] = (path, value)
            except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
                continue
        return out

    @app.get("/experiments")
    def index(name: str = "", state: str = ""):
        records = experiments.list_runs(workdir)
        rows = []
        for row in records:
            if name and name.casefold() not in row["name"].casefold():
                continue
            if state and row["state"] != state:
                continue
            summary = {}
            if row["state"] == "succeeded":
                try:
                    summary = experiments.read(Path(row["path"]) / "metrics.json")
                except (OSError, ValueError):
                    pass
            final = summary.get("ic", {}).get(summary.get("model_key", "lgbm_ensemble"), {}).get("final_test", {})
            ic = f"{final['mean']:+.4f}" if isinstance(final.get("mean"), (int, float)) else "—"
            evaluations = experiments.list_evaluations(workdir, row["run_id"])
            rows.append(f'<tr><td><a href="/experiments/{row["run_id"]}">{esc(row["name"])}</a>'
                        f'<br><code>{row["run_id"][:12]}</code></td>'
                        f'<td>{esc(STATE_LABELS.get(row["state"], row["state"]))}</td>'
                        f'<td>{esc(row["model"])}</td><td>{esc(row["created_at"][:19])}</td>'
                        f'<td>{ic}</td><td>{len(evaluations)}</td></tr>')
        states = '<option value="">全部状态</option>' + ''.join(
            f'<option value="{key}"{" selected" if state == key else ""}>{label}</option>'
            for key, label in STATE_LABELS.items())
        body = ('<h2>实验记录</h2><p><a href="/experiments/compare">比较 Bench 结果</a></p>'
                f'<form method="get"><label class="f">名称 <input name="name" value="{esc(name)}"></label>'
                f'<label class="f">状态 <select name="state">{states}</select></label><button>筛选</button></form>'
                '<div class="tw"><table><thead><tr><th>训练版本</th><th>状态</th><th>模型</th>'
                '<th>开始时间 UTC</th><th>最终期 Rank IC</th><th>Bench 次数</th></tr></thead><tbody>'
                + (''.join(rows) or '<tr><td colspan="6">暂无训练记录</td></tr>') + '</tbody></table></div>')
        legacy = []
        for root in sorted((workdir / "runs").iterdir()):
            if (root / "metrics.json").is_file() and not (root / "run.json").exists():
                legacy.append(f'<li>{esc(root.name)} <form class="inline" method="post" action="/experiments/import">'
                              f'<input type="hidden" name="token" value="{token}">'
                              f'<input type="hidden" name="name" value="{esc(root.name)}"><button>纳入历史记录</button></form></li>')
        if legacy:
            body += '<h3>未纳入记录的历史结果</h3><ul>' + ''.join(legacy) + '</ul>'
        return page("实验记录", body)

    @app.post("/experiments/import")
    async def import_old(req: Request):
        form = await req.form()
        guard(req, form.get("token"))
        name = str(form.get("name", ""))
        root = workdir / "runs" / name
        if not name or root.resolve().parent != (workdir / "runs").resolve():
            raise HTTPException(400, "无效历史目录")
        try:
            record = experiments.import_legacy(root, workdir)
        except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse(f'/experiments/{record["run_id"]}', 303)

    @app.get("/experiments/compare")
    def compare_page(old: str = "", new: str = ""):
        rows = experiments.list_evaluations(workdir)
        runs = {row["run_id"]: row for row in experiments.list_runs(workdir)}
        choices = {}
        candidates = []
        for row in rows:
            try:
                score = experiments.read(evaluation(row["evaluation_id"]) / "scorecard.json")
                if score["status"] == "COMPLETED" and score["evaluation"]["comparable"]:
                    run = runs.get(row.get("run_id"), {})
                    choices[row["evaluation_id"]] = f'{run.get("name", score["task"])} · {run.get("model", "外部预测")} · {row["created_at"][:19]} · {row["evaluation_id"][:12]}'
                    candidates.append(row)
            except (OSError, ValueError, KeyError, HTTPException):
                continue
        if candidates:
            new = new or candidates[0]["evaluation_id"]
            current = next((row for row in candidates if row["evaluation_id"] == new), candidates[0])
            prior = next((row for row in candidates if row.get("run_id") != current.get("run_id")
                          and row["protocol_hash"] == current["protocol_hash"]), candidates[-1])
            old = old or prior["evaluation_id"]
        def options(selected):
            return ''.join(f'<option value="{key}"{" selected" if key == selected else ""}>{esc(label)}</option>'
                           for key, label in choices.items())
        body = '<h2>比较 Bench 结果</h2><p><a href="/experiments">返回实验记录</a></p>'
        if not choices:
            return page("比较结果", body + '<p>暂无可比较的 Bench 结果。</p>')
        body += (f'<form method="post" action="/experiments/compare"><input type="hidden" name="token" value="{token}">'
                 f'<label class="f">旧版本 <select name="old" required>{options(old)}</select></label><br>'
                 f'<label class="f">新版本 <select name="new" required>{options(new)}</select></label><br>'
                 '<button>运行配对比较</button></form>')
        return page("比较结果", body)

    @app.post("/experiments/compare")
    async def compare(req: Request):
        form = await req.form()
        guard(req, form.get("token"))
        old, new = evaluation(str(form.get("old", ""))), evaluation(str(form.get("new", "")))
        from .bench.runner import load_scored_run
        try:
            a, _ = load_scored_run(old)
            b, _ = load_scored_run(new)
            if a["protocol_hash"] != b["protocol_hash"]:
                raise ValueError("协议不同，不能直接比较；请在同一协议下重新评价")
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from None
        jid = submit("evaluate", {"action": "compare", "old": str(old), "new": str(new),
                                  "ui": {"name": "版本比较", "what": "compare"}})
        return RedirectResponse(f"/job/{jid}", 303)

    @app.get("/experiments/{run_id}")
    def detail(run_id: str):
        root, record = locate(run_id)
        body = (f'<p><a href="/experiments">返回实验记录</a></p><h2>{esc(record["name"])}</h2>'
                f'<p>{esc(STATE_LABELS.get(record["state"], record["state"]))} · {esc(record["model"])}</p>'
                '<dl class="identity">' + ''.join(f'<dt>{label}</dt><dd><code>{esc(record.get(key))}</code></dd>'
                for key, label in (("run_id", "运行"), ("experiment_id", "实验"), ("model_version", "模型版本"),
                                   ("prediction_version", "折预测版本"), ("config_hash", "配置"),
                                   ("data_snapshot_id", "数据"), ("origin", "记录来源"))) + '</dl>')
        if record.get("error"):
            body += f'<pre class="plain">{esc(record["error"])}</pre>'
        if record["state"] != "succeeded":
            return page("训练记录", body)
        try:
            experiments.verify(root)
        except (OSError, ValueError, KeyError) as exc:
            return page("产物校验失败", body + f'<p class="warn">{esc(exc)}</p>')
        model_link = f'<a href="/models/{run_id}">查看模型信号</a> · ' if (root / "final_model" / "meta.json").is_file() else ''
        body += f'<p>{model_link}<a href="/experiments/{run_id}/evidence.zip">导出研究摘要</a></p>'
        available = protocols()
        if available:
            options = ''.join(f'<option value="{esc(key)}">{esc(key)}</option>' for key in available)
            body += (f'<h3>独立 Bench</h3><form method="post" action="/experiments/{run_id}/bench">'
                     f'<input type="hidden" name="token" value="{token}">'
                     f'<label class="f">评价协议 <select name="protocol" required>{options}</select></label>'
                     '<button>运行 Bench</button></form>')
        else:
            body += '<p class="mut">暂无评价协议</p>'
        if record["model"] == "lightgbm":
            body += (f'<details><summary>以此模型冻结新协议</summary><form method="post" action="/experiments/{run_id}/protocol">'
                     f'<input type="hidden" name="token" value="{token}">'
                     '<label class="f">日期块长度 <input type="number" name="block_days" value="20" min="1" max="252" required></label>'
                     '<label class="f">重采样次数 <input type="number" name="resamples" value="10000" min="100" max="100000" required></label>'
                     '<label class="f">最少日期 <input type="number" name="min_days" value="60" min="2" required></label>'
                     '<button>冻结开发协议</button></form></details>')
        rows = []
        for row in experiments.list_evaluations(workdir, run_id):
            rows.append(f'<tr><td><a href="/evaluations/{row["evaluation_id"]}">{row["evaluation_id"][:12]}</a></td>'
                        f'<td>{esc(row["status"])}</td><td>{esc(row["created_at"][:19])}</td>'
                        f'<td><code title="{esc(row["protocol_hash"])}">{esc(row["protocol_hash"][:19])}</code></td></tr>')
        body += ('<div class="tw"><table class="evaluation-list"><tr><th>评价</th><th>状态</th><th>时间 UTC</th><th>协议</th></tr>'
                 + (''.join(rows) or '<tr><td colspan="4">尚未运行 Bench</td></tr>') + '</table></div>'
                 '<details><summary>训练报告</summary>' + markdown((root / "report.md").read_text(encoding="utf-8")) + '</details>')
        return page("训练记录", body)

    @app.post("/experiments/{run_id}/protocol")
    async def new_protocol(req: Request, run_id: str):
        form = await req.form()
        guard(req, form.get("token"))
        root, record = locate(run_id)
        try:
            experiments.verify(root)
            if record["model"] != "lightgbm":
                raise ValueError("冻结锚点需要 LightGBM 模型")
            settings = {key: int(str(form.get(key, default))) for key, default in
                        (("block_days", 20), ("resamples", 10000), ("min_days", 60))}
            if not (1 <= settings["block_days"] <= 252 and 100 <= settings["resamples"] <= 100000 and settings["min_days"] >= 2):
                raise ValueError("无效统计设置")
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from None
        filename = f"development-{run_id}-{uuid.uuid4().hex[:8]}.yaml"
        jid = submit("evaluate", {"action": "protocol", "anchor": str(root), "filename": filename,
                                  "statistics": settings,
                                  "ui": {"name": record["name"], "what": "protocol", "run_id": run_id}})
        return RedirectResponse(f"/job/{jid}", 303)

    @app.post("/experiments/{run_id}/bench")
    async def bench(req: Request, run_id: str):
        form = await req.form()
        guard(req, form.get("token"))
        root, _ = locate(run_id)
        try:
            experiments.verify(root)
            selected = protocols().get(str(form.get("protocol", "")))
            if selected is None:
                raise ValueError("评价协议不存在")
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from None
        jid = submit("evaluate", {"action": "bench", "source": str(root), "protocol": str(selected[0]),
                                  "ui": {"name": experiments.read(root / "run.json").get("name", run_id[:12]),
                                         "what": "bench", "run_id": run_id}})
        return RedirectResponse(f"/job/{jid}", 303)

    @app.get("/evaluations/{evaluation_id}")
    def evaluation_page(evaluation_id: str):
        root = evaluation(evaluation_id)
        from .bench.runner import verify_result
        try:
            verify_result(root)
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from None
        score = experiments.read(root / "scorecard.json")
        run_id = (score.get("lineage") or {}).get("run_id")
        body = '<p><a href="/experiments">返回实验记录</a> · <a href="/experiments/compare">比较结果</a></p>'
        if run_id:
            from .workflow_ui import reason_panel, score_summary
            summary = score_summary(score)
            body += (f'<div class="page-heading"><h1>{summary["label"]}</h1></div>'
                     f'<p>{esc(summary["message"])}</p>'
                     '<p class="notice">此结论不代表可投入实盘。证据不足时，纸面账户仅用于继续观察。</p>'
                     f'<div class="actions"><a class="btn" href="/workflow/{run_id}/paper">下一步：纸面交易</a>'
                     f'<a href="/workflow/{run_id}">继续训练 / 调参 / 删除模型</a></div>')
            body += reason_panel(score)
            body += (f'<p><a href="/experiments/{run_id}">对应训练版本</a> · '
                     f'<a href="/experiments/{run_id}/evidence.zip?evaluation_id={evaluation_id}">导出含 Bench 的研究摘要</a></p>')
        body += markdown((root / "report.md").read_text(encoding="utf-8"))
        return page("Bench 结果", body)

    @app.get("/comparisons/{comparison_id}")
    def comparison_page(comparison_id: str):
        if not experiments.ID_RE.fullmatch(comparison_id):
            raise HTTPException(404)
        root = workdir / "runs" / "comparisons" / comparison_id
        if not (root / "report.md").is_file():
            raise HTTPException(404)
        return page("版本比较", '<p><a href="/experiments/compare">返回版本选择</a></p>'
                    + markdown((root / "report.md").read_text(encoding="utf-8")))

    @app.get("/experiments/{run_id}/evidence.zip")
    def export(run_id: str, evaluation_id: str = ""):
        root, _ = locate(run_id)
        try:
            payload = evidence_export.build(root, evaluation(evaluation_id) if evaluation_id else None)
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from None
        return Response(payload, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="temperq-evidence-{run_id}.zip"'})
