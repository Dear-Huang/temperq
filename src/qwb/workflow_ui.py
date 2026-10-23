"""Novice model lifecycle: train, evaluate, paper trade, preview actual holdings."""
from __future__ import annotations

import copy
import html
import uuid
from pathlib import Path

import yaml
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import account_advice, experiments, paper, paper_auto, picks, runconfig, strategy


def esc(value):
    return html.escape(str(value))


def hidden(workdir, source):
    return (Path(workdir) / "runs" / "workflow" / "hidden" / f"{source.name}.json").is_file()


def evaluation_summary(workdir, source):
    """Use the latest registered result, never replace it with a better older score."""
    record = experiments.read(source / "run.json") if (source / "run.json").exists() else {}
    rows = experiments.list_evaluations(workdir, record.get("run_id", source.name))
    if not rows:
        return {"label": "待跑基准", "tone": "pending", "ready": False,
                "message": "训练已完成，尚未进行独立基准评价。", "score": None}
    row = rows[0]
    try:
        from .bench.runner import verify_result
        root = experiments.evaluation_dir(workdir, row["evaluation_id"])
        verify_result(root)
        score = experiments.read(root / "scorecard.json")
        return dict(score_summary(score), score=score, evaluation_id=row["evaluation_id"])
    except (OSError, ValueError, KeyError):
        return {"label": "评价受阻", "tone": "blocked", "ready": False, "score": None,
                "message": "最近评价的产物校验失败，请重新运行基准。"}


def score_summary(score):
    ev = score.get("evaluation") or {}
    if score.get("status") != "COMPLETED" or ev.get("comparable") is not True:
        return {"label": "评价输入不合格", "tone": "blocked", "ready": False,
                "message": "先修复数据、预测覆盖或训练记录，当前结果不能判断模型好坏。"}
    ready = (score.get("status") == "COMPLETED" and ev.get("comparable") is True and
             score.get("readiness", {}).get("level") == "SIGNAL_RESEARCH_CANDIDATE")
    candidate = ev.get("models", {}).get("candidate", {})
    if candidate.get("ci95") is None and candidate.get("ci_reason") == "insufficient_dates_or_valid_blocks":
        message = f'有效检验日期不足（当前 {candidate.get("n", 0)} 日），无法计算可靠区间；请补充独立日期，不要只增加股票行数或重复调参。'
    else:
        message = "尚未确认稳定有效；可调参重训，或仅用纸面账户继续观察。"
    return {"label": "可进入纸面验证" if ready else "证据不足", "tone": "active" if ready else "pending",
            "ready": ready, "message": "历史信号达到研究门槛，下一步观察纸面账户。" if ready else
            message}


def reason_panel(score):
    """Separate signal, costs and data problems without implying a live certification."""
    ev = score.get("evaluation") or {}
    gates = {gate["id"]: gate for gate in score.get("readiness", {}).get("gates", [])}
    labels = {"PASS": "达到当前门槛", "FAIL": "未达到门槛", "INCONCLUSIVE": "证据不足",
              "NOT_EVALUATED": "尚未检验", "INVALID": "输入不合格"}
    rows = []
    for key, title, meaning in (
        ("comparable_input", "数据与记录", "缺预测、时点或训练记录时先修输入，不算模型失效。"),
        ("positive_signal", "预测能力", "看排序信号及区间；不显著不等于已经证明无效。"),
        ("net_account", "扣成本超额", "看是否优于基准；未胜过基准不等于账户绝对亏损。"),
    ):
        if key in gates:
            rows.append(f'<tr><td>{title}</td><td>{labels.get(gates[key]["status"], "待确认")}</td><td>{meaning}</td></tr>')
    detail = ''
    candidate = ev.get("models", {}).get("candidate", {})
    if candidate.get("n") is not None:
        detail += f'<p>本次有效预测检验日期：{esc(candidate["n"])} 日。更多股票不能替代更多独立日期。</p>'
    if score.get("status") != "COMPLETED":
        detail += '<p class="warn">评价未完成。请先检查数据范围、字段和冻结协议；不据此给模型判好坏。</p>'
    return (('<div class="tw"><table><tr><th>检查内容</th><th>状态</th><th>怎么理解</th></tr>' +
             ''.join(rows) + '</table></div>') if rows else '') + detail


def register(app, workdir, *, page, guard, token, locate, submit):
    def resolve(run):
        source = locate(run)
        return source, picks.load(source)

    def available_protocols(meta):
        from .bench.contracts import UNIVERSE_KEYS, validate_protocol
        result = {}
        for path in sorted((workdir / "bench" / "protocols").glob("*.yaml")):
            try:
                protocol = yaml.safe_load(path.read_text("utf-8"))
                validate_protocol(protocol)
                if (protocol["task"] != meta["card"] or
                        protocol["data"]["snapshot_id"] != meta["snapshot_id"] or
                        protocol["evaluation_years"] != meta["cfg"]["final_test_years"] or
                        sorted(protocol["universe"].get("universe_list", [])) != sorted(meta["cfg"].get("universe_list", [])) or
                        any(protocol["universe"].get(key) != meta["cfg"].get(key) for key in UNIVERSE_KEYS) or
                        protocol["universe"].get("min_adv_yuan", 0) != (meta["cfg"].get("min_adv_yuan") or 0) or
                        not Path(protocol["anchor"]["run_dir"]).is_dir()):
                    continue
                result[path.name] = path
            except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
                continue
        return result

    def bound_accounts(meta):
        model_hash = strategy.model_identity(meta["dir"])
        matches = []
        for path in (workdir / "runs" / "paper").glob("*/strategy.json"):
            try:
                if experiments.read(path)["model_hash"] == model_hash:
                    matches.append(path.parent.name)
            except (OSError, ValueError, KeyError):
                continue
        return matches

    @app.get("/workflow/{run}")
    def model_home(run: str):
        source, meta = resolve(run)
        display = experiments.read(source / "run.json").get("name", run) if (source / "run.json").is_file() else run
        summary = evaluation_summary(workdir, source)
        synthetic = meta["data"].get("source") == "synthetic"
        body = (f'<p class="breadcrumb"><a href="/models">评价模型</a> / 模型详情</p>'
                f'<div class="page-heading"><div><h1>{esc(display)}</h1>'
                f'<p class="mut">{esc(meta["model"])} · 训练数据到 {esc(meta.get("train_end", "未知"))}</p></div>'
                f'<span class="status-badge {summary["tone"]}">{summary["label"]}</span></div>')
        if synthetic:
            body += '<p class="notice warn">试运行模型：合成数据只验证操作流程，不支持真实前向纸面交易。</p>'
        if hidden(workdir, source):
            body += (f'<p class="notice">此模型已从常用列表删除，历史评价和账户仍保留。</p>'
                     f'<form method="post" action="/workflow/{esc(run)}/visibility">'
                     f'<input type="hidden" name="token" value="{token}"><button name="restore" value="yes">恢复模型</button></form>')
        body += (f'<section class="data-section"><h2>下一步：评价模型</h2><p>{esc(summary["message"])}</p>'
                 '<p class="mut">评价通过仅表示可开始纸面验证，不是实盘准入或收益承诺。</p>')
        if summary.get("score"):
            body += reason_panel(summary["score"])
        if summary.get("evaluation_id"):
            body += f'<p><a class="btn" href="/evaluations/{summary["evaluation_id"]}">查看基准结果</a></p>'
        protocols = available_protocols(meta)
        can_initialize = meta["model"] == "lightgbm" and not synthetic
        if protocols or can_initialize:
            body += (f'<form method="post" action="/workflow/{esc(run)}/bench">'
                     f'<input type="hidden" name="token" value="{token}">'
                     '<label class="f">基准任务 <select name="protocol">' +
                     ('<option value="auto">建立本模型的固定基准任务</option>' if not protocols and can_initialize else '') + ''.join(
                         f'<option value="{esc(key)}">{int(meta["card"]["target"]["horizon_days"])} 日预测 · 同数据固定基准 {i+1}</option>'
                         for i, key in enumerate(protocols)) +
                     '</select></label><button>跑基准</button></form>')
        else:
            body += ('<p class="warn">合成试运行不作真实基准评价。</p>' if synthetic else
                     '<p class="warn">尚无同数据、同任务的 LightGBM 基准。请先训练一个 LightGBM 作为固定参考，再评价此模型。</p>')
        body += '</section><section class="data-section"><h2>继续训练或调整参数</h2>'
        body += (f'<form method="post" action="/workflow/{esc(run)}/retrain">'
                 f'<input type="hidden" name="token" value="{token}">'
                 f'<label class="f">新版本名称 <input name="name" value="{esc(display[:23])}_v_{uuid.uuid4().hex[:6]}" '
                 'pattern="[A-Za-z0-9_-]{1,40}" maxlength="40" required></label>')
        if meta["model"] == "lightgbm":
            body += (f'<label class="f">训练轮数 <input type="number" name="rounds" min="1" max="10000" value="{meta["cfg"]["num_boost_round"]}" required></label>'
                     '<details><summary>调整训练参数</summary>'
                     f'<label class="f">学习率 <input type="number" name="learning_rate" min="0.00001" max="1" step="any" value="{meta["cfg"]["lgbm"]["learning_rate"]}" required></label>'
                     f'<label class="f">叶子数 <input type="number" name="num_leaves" min="2" max="1024" value="{meta["cfg"]["lgbm"]["num_leaves"]}" required></label></details>')
        elif meta["model"] == "ridge":
            body += (f'<label class="f">正则强度 <input type="number" name="alpha" min="0.00001" step="any" value="{meta["cfg"].get("ridge_alpha", 1)}" required></label>')
        body += '<p class="mut">沿用原数据、特征和评价区间，完整重训为新版本；保留旧模型，当前不支持断点续训或增量微调。</p><button>继续训练 · 创建新版本</button></form></section>'
        accounts = bound_accounts(meta)
        body += '<section class="data-section"><h2>纸面交易</h2>'
        if accounts:
            body += '<div class="actions">' + ''.join(f'<a class="btn" href="/paper/{esc(name)}">{esc(name)} · 查看运行结果</a>' for name in accounts) + '</div>'
        if not synthetic:
            body += f'<p><a class="btn" href="/workflow/{esc(run)}/paper">创建纸面账户</a></p>'
        body += ('</section><details><summary>报告与模型管理</summary>'
                 f'<p><a href="/models/{esc(run)}">原始信号清单</a> · <a href="/experiments">实验记录与版本比较</a></p>'
                 f'<form method="post" action="/workflow/{esc(run)}/visibility">'
                 f'<input type="hidden" name="token" value="{token}"><button class="secondary">删除模型（可恢复）</button>'
                 '<p class="mut">从常用列表移除；历史证据和已经绑定的纸面账户保留。</p></form></details>')
        return page("评价模型", body)

    @app.post("/workflow/{run}/visibility")
    async def visibility(req: Request, run: str):
        form = await req.form()
        guard(req, form.get("token"))
        source, _ = resolve(run)
        root = workdir / "runs" / "workflow" / "hidden"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{source.name}.json"
        if form.get("restore") == "yes":
            path.unlink(missing_ok=True)
        else:
            experiments.write(path, {"source": str(source), "deleted_at": experiments.now()})
        return RedirectResponse("/models", 303)

    @app.post("/workflow/{run}/retrain")
    async def retrain(req: Request, run: str):
        form = await req.form()
        guard(req, form.get("token"))
        source, meta = resolve(run)
        try:
            name = str(form.get("name", ""))
            if not paper.NAME.fullmatch(name) or (workdir / "runs" / name).exists():
                raise ValueError("请输入未使用过的版本名称（1–40 位字母、数字、下划线或连字符）")
            cfg = copy.deepcopy(meta["cfg"])
            model = {"name": meta["model"]}
            if meta["model"] == "lightgbm":
                rounds, leaves = int(form.get("rounds", 0)), int(form.get("num_leaves", 0))
                lr = float(form.get("learning_rate", 0))
                if not (1 <= rounds <= 10000 and 2 <= leaves <= 1024 and 0 < lr <= 1):
                    raise ValueError("训练轮数、学习率或叶子数超出范围")
                model.update(num_boost_round=rounds, params=dict(cfg["lgbm"], num_leaves=leaves, learning_rate=lr))
                if cfg.get("selection"):
                    model["selection"] = cfg["selection"]
            elif meta["model"] == "ridge":
                model["alpha"] = float(form.get("alpha", 0))
            raw = {"name": name, "task": meta["card"], "data": meta["data"], "model": model,
                   "train": {key: value for key, value in cfg.items() if key in runconfig.TRAIN}}
            resolved, report = runconfig.resolve(raw, workdir)
            if resolved is None:
                raise ValueError(str(report))
            jid = submit("train", {"action": "train", "resolved": resolved, "notes": [],
                                    "config_hash": resolved["config_hash"],
                                    "ui": {"name": name, "what": "train", "parent": str(source)}})
            return RedirectResponse(f"/job/{jid}", 303)
        except (ValueError, TypeError) as exc:
            return page("未开始训练", f'<p class="bad">{esc(exc)}</p><a href="/workflow/{esc(run)}">返回模型</a>')

    @app.post("/workflow/{run}/bench")
    async def bench(req: Request, run: str):
        form = await req.form()
        guard(req, form.get("token"))
        source, meta = resolve(run)
        auto = form.get("protocol") == "auto" and meta["model"] == "lightgbm" and meta["data"].get("source") != "synthetic"
        protocol = available_protocols(meta).get(str(form.get("protocol", "")))
        if protocol is None and not auto:
            raise HTTPException(400, "基准任务不存在")
        try:
            if not (source / "run.json").exists():
                record = experiments.import_legacy(source, workdir)
                source = experiments.run_dir(workdir, record["run_id"])
            experiments.verify(source)
            if auto:
                protocol = workdir / "bench" / "protocols" / f"model-{source.name}.yaml"
            jid = submit("evaluate", {"action": "bench", "source": str(source), "protocol": str(protocol),
                                      "create_protocol": auto,
                                      "ui": {"name": run, "what": "bench"}})
            return RedirectResponse(f"/job/{jid}", 303)
        except (ValueError, OSError, KeyError) as exc:
            return page("基准未开始", f'<p class="bad">{esc(exc)}</p><a href="/workflow/{esc(run)}">返回模型</a>')

    @app.get("/workflow/{run}/paper")
    def paper_setup(run: str):
        source, meta = resolve(run)
        summary = evaluation_summary(workdir, source)
        if not summary.get("score") or summary["score"].get("status") != "COMPLETED":
            return page("请先评价模型", f'<h1>请先跑完基准</h1><p>{esc(summary["message"])}</p><a class="btn" href="/workflow/{esc(run)}">返回模型评价</a>')
        return page("创建纸面账户", f'<p><a href="/workflow/{esc(run)}">返回模型评价</a></p>'
                    f'<h1>创建纸面账户</h1><p>{esc(run)} · {summary["label"]}</p>'
                    '<p class="notice">使用虚拟资金，冻结当前模型；真实现金和仓位在下一步“模拟持仓”中输入。</p>'
                    f'<form method="post"><input type="hidden" name="token" value="{token}">'
                    f'<label class="f">账户名称 <input name="name" value="paper_{uuid.uuid4().hex[:8]}" pattern="[A-Za-z0-9_-]{{1,40}}" required></label>'
                    '<label class="f">虚拟资金（元） <input name="cash" type="number" min="1" value="100000" step="0.01" required></label>'
                    f'<label class="f">每日行情目录 <input name="feed" value="{esc(meta["data"].get("dir") or "")}" required></label>'
                    '<p class="mut">使用模型对应的完整行情和交易日历；CSV 数据需持续更新。</p>'
                    '<label class="row"><input type="checkbox" name="observe_only" value="yes" required>仅作纸面观察，理解证据不足不代表模型已有效</label>'
                    '<label class="row"><input type="checkbox" name="automatic" value="yes" checked>每日 17:30 后自动记账（工作台须保持运行）</label>'
                    '<p><button>开始纸面交易</button></p></form>')

    @app.post("/workflow/{run}/paper")
    async def create_paper(req: Request, run: str):
        form = await req.form()
        guard(req, form.get("token"))
        source, _ = resolve(run)
        try:
            summary = evaluation_summary(workdir, source)
            if not summary.get("score") or summary["score"].get("status") != "COMPLETED":
                raise ValueError("请先完成独立基准评价，再开始纸面观察")
            if form.get("observe_only") != "yes":
                raise ValueError("请确认仅作纸面观察")
            feed = Path(str(form.get("feed", "")))
            feed = feed if feed.is_absolute() else workdir / feed
            if not feed.is_dir():
                raise ValueError("行情目录不存在")
            from . import daily_data
            actual = daily_data.current(feed) if (feed / "current.json").exists() else feed
            if not (actual / "calendar.csv").is_file():
                raise ValueError("行情目录缺少 calendar.csv 交易日历，请先更新数据")
            name = str(form.get("name", ""))
            paper.initialize(workdir, name, source, float(form.get("cash", "")), [], data_dir=feed)
            experiments.write(paper.location(workdir, name) / "workflow.json", {
                "run": run, "evaluation_id": summary["evaluation_id"], "created_at": experiments.now()})
            paper_auto.configure(workdir, name, form.get("automatic") == "yes")
            return RedirectResponse(f"/paper/{name}", 303)
        except (ValueError, OSError, KeyError) as exc:
            return page("未创建纸面账户", f'<p class="bad">{esc(exc)}</p><a href="/workflow/{esc(run)}/paper">返回修改</a>')

    @app.get("/holdings")
    def holdings_index():
        rows = []
        for path in sorted((workdir / "runs" / "paper").glob("*/strategy.json")):
            value = paper.inspect(workdir, path.parent.name)
            if value["config"]["scope"] != "forward_paper":
                continue
            name = path.parent.name
            rows.append(f'<tr><td>{esc(name)}</td><td>{len(value["sessions"])} 日</td>'
                        f'<td><a href="/paper/{esc(name)}">运行结果</a></td><td><a class="btn" href="/holdings/{esc(name)}">模拟真实持仓</a></td></tr>')
        return page("模拟持仓", '<h1>模拟持仓</h1><p class="notice">输入真实现金和持仓，查看冻结模型的参考建议；不修改纸面账本，不向券商下单。</p>'
                    '<div class="tw"><table><tr><th>纸面账户</th><th>已观察</th><th>历史</th><th>操作</th></tr>' +
                    (''.join(rows) or '<tr><td colspan="4">尚无前向账户。<a href="/paper">先开始纸面交易</a></td></tr>') + '</table></div>')

    @app.get("/holdings/{name}")
    def holdings_form(name: str):
        try:
            value = paper.inspect(workdir, name)
        except (ValueError, OSError):
            raise HTTPException(404) from None
        return page("模拟持仓", f'<p><a href="/paper/{esc(name)}">返回纸面运行结果</a></p><h1>模拟持仓</h1>'
                    f'<p>绑定账户：{esc(name)} · 已记录 {len(value["sessions"])} 日</p>'
                    '<p class="notice">此处使用该账户的冻结模型及每日行情。建议仅供模拟，尚未证明可实盘；不会写入纸面账本。</p>'
                    f'<form method="post" enctype="multipart/form-data" onsubmit="this.querySelector(\'button\').disabled=true;this.querySelector(\'button\').textContent=\'正在计算…\'"><input type="hidden" name="token" value="{token}">'
                    '<label class="f">真实可用现金（元） <input name="cash" type="number" min="0" step="0.01" required></label>'
                    '<label class="f">计划执行日 <input name="execution_day" type="date" required></label>'
                    '<label class="f">持仓 CSV <input name="file" type="file" accept=".csv"></label><p><a href="/account-template.csv">下载持仓模板</a> · 空仓可不上传</p>'
                    '<label class="row"><input type="checkbox" name="supported_only" value="yes" required>已确认无待处理公司行动、北交所持仓或特殊订单</label>'
                    '<p><button>模拟模型建议</button></p></form>')

    @app.post("/holdings/{name}")
    async def holdings_preview(req: Request, name: str):
        form = await req.form()
        guard(req, form.get("token"))
        try:
            if form.get("supported_only") != "yes":
                raise ValueError("请先确认账户支持范围")
            upload = form.get("file")
            raw = await upload.read(1_000_001) if hasattr(upload, "read") else b""
            if len(raw) > 1_000_000:
                raise ValueError("持仓 CSV 不得超过 1 MB")
            holdings = strategy.read_holdings(raw)
            result = await run_in_threadpool(account_advice.preview, workdir, name,
                                             float(form.get("cash", "")), holdings,
                                             str(form.get("execution_day", "")))
            output = workdir / "runs" / "advice"
            output.mkdir(parents=True, exist_ok=True)
            advice_id = uuid.uuid4().hex
            experiments.write(output / f"{advice_id}.json", result)
            return RedirectResponse(f"/advice/{advice_id}", 303)
        except (ValueError, OSError, KeyError) as exc:
            return page("未生成模拟建议", f'<p class="bad">{esc(exc)}</p><a href="/holdings/{esc(name)}">返回修改</a>')
