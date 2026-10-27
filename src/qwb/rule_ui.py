"""Point-and-click rule editing with explicit staged import confirmation."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import uuid

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import experiments as ex, experience_ui, rule_cards, rule_signals, strategy_projects as projects, strategy_workspace as ws, daily_data


def register(app, workdir, *, page, guard, token, datasets):
    esc = experience_ui.esc
    def hidden(pid=""):
        return f'<input type="hidden" name="token" value="{token}"><input type="hidden" name="project" value="{esc(pid)}">'

    def error(exc, pid=""):
        response = page("需要调整", '<h1>请检查这一项</h1><p class="warn">'+esc(exc)+'</p>'
            + f'<a href="/playbooks/setup?project={esc(pid)}">返回规则编辑</a>' if pid else '<h1>请检查规则文件</h1><p>'+esc(exc)+'</p><a href="/playbooks">返回模板</a>')
        response.status_code = 400
        return response

    def available():
        return {k: v for k, v in datasets(workdir).items() if k != "_trial" and v.get("path") is not None}

    @app.post('/rules/revise')
    async def revise(req: Request):
        form=await req.form();guard(req,form.get('token'))
        try:
            sid,vid=str(form['sid']),str(form['vid'])
            root,spec=ws.revision(workdir,sid,vid,verify_implementation=False)
            meta=ex.read(root/'final_model/meta.json')
            if meta.get('model')!='rule_card':raise ValueError('此版本没有经验规则卡')
            doc=rule_cards.validate(meta['rule_card'])
            pid=projects.create(workdir,route='rules',name=doc['name'],sid=sid,parent=vid)
            matching=next((k for k,v in available().items() if daily_data.current(v['path'])==daily_data.current(spec['data_feed'])), '')
            projects.update(workdir,pid,rule_document=doc,dataset=matching,stage='rule_editor',
                            rule_parent=Path(spec['signal']['source']).name,
                            rule_series=ex.read(root/'model-evaluation.json').get('rule_series',pid)
                                if (root/'model-evaluation.json').exists() else pid)
            return RedirectResponse('/playbooks/setup?project='+pid,303)
        except (ValueError,OSError,KeyError,TypeError) as exc:return error(exc)

    @app.get("/playbooks")
    def catalog(project: str = ""):
        if project: projects.read(workdir, project)
        body = '<h1>选一个起点</h1><section class="rule-templates">'
        for doc in rule_cards.catalog():
            body += f'<article><h2>{esc(doc["name"])}</h2><ul>'+''.join(f'<li>{esc(r["description"])}</li>' for r in doc['rules'])+'</ul>'
            body += '<form method="post" action="/playbooks/select">'+hidden(project)+f'<input type="hidden" name="playbook" value="{doc["id"]}"><button class="secondary">选择</button></form></article>'
        body += '</section><details><summary>导入自己的规则卡</summary><form method="post" action="/rules/import" enctype="multipart/form-data">'+hidden(project)+'<label class="f">规则卡 JSON<input type="file" name="rules" accept=".json" required></label><button>校验并预览</button></form></details>'
        return page("经验策略", body)

    @app.post("/playbooks/select")
    async def select(req: Request):
        form = await req.form(); guard(req, form.get("token"))
        try:
            aliases = {"first_strategy": "trend_following_daily", "patient_owner": "low_volatility_daily", "active_rebalancer": "volume_price_daily"}
            key = str(form.get("playbook", ""))
            doc = rule_cards.template(aliases.get(key, key))
            pid = str(form.get("project", "")) or projects.create(workdir, route="rules", name=doc["name"])
            draft = projects.read(workdir, pid)
            if draft.get("rule_document"):
                projects.update(workdir, pid, pending_rules=doc)
                return RedirectResponse(f"/rules/confirm?project={pid}", 303)
            projects.update(workdir, pid, rule_document=doc, route="rules", stage="rule_editor", name=doc["name"])
            return RedirectResponse(f"/playbooks/setup?project={pid}", 303)
        except (ValueError, OSError, KeyError) as exc: return error(exc)

    @app.get("/playbooks/setup")
    def editor(project: str, edit: int = 0):
        try:
            draft = projects.read(workdir, project)
            doc = rule_cards.validate(draft["rule_document"])
        except (ValueError, OSError, KeyError) as exc: return error(exc)
        body = experience_ui.flow(1, project=project)+f'<div class="page-heading"><h1>{esc(doc["name"])}</h1><a href="/rules/{project}.json">导出规则卡</a></div>'
        body += '<form method="post" action="/playbooks/setup">'+hidden(project)
        body += f'<label class="f">策略名称<input name="name" maxlength="80" value="{esc(doc["name"])}" required></label>'
        for i, rule in enumerate(doc["rules"]):
            body += f'<details class="rule-row" data-rule="{i}" {"open" if i==edit else ""}><summary>{esc(rule["description"])} <small>{"必须满足" if rule["required"] else "加分"} · 权重 {rule["weight"]:g}</small></summary><div class="rule-fields">'
            for key, title, choices in (("field", "指标", rule_cards.FIELDS), ("operator", "条件", rule_cards.OPERATORS)):
                body += f'<label>{title}<select name="r{i}_{key}">'+''.join(f'<option value="{k}" {"selected" if rule[key]==k else ""}>{v}</option>' for k, v in choices.items())+'</select></label>'
            for key, title, low, high, step in (("window", "交易日窗口", 1, 504, "1"), ("value", "附加数值", -100, 504, "any"), ("weight", "分值权重", -10, 10, "any")):
                body += f'<label>{title}<input type="number" name="r{i}_{key}" value="{rule[key]}" min="{low}" max="{high}" step="{step}" required></label>'
            body += f'<label class="rule-required"><input type="checkbox" name="r{i}_required" {"checked" if rule["required"] else ""}>必须满足</label>'
            body += f'<span class="mut">来源：{esc(rule["origin"])}</span><button class="rule-remove secondary" name="action" value="remove_{i}" title="删除规则" aria-label="删除规则">×</button></div></details>'
        body += '<div class="policy-actions"><button class="secondary" name="action" value="add">添加条件</button><button class="secondary" name="action" value="templates">更换模板 / 导入</button></div>'
        options = available()
        if options:
            body += '<label class="f">行情数据<select name="dataset">'+''.join(f'<option value="{esc(k)}" {"selected" if k==draft.get("dataset") else ""}>{esc(v.get("label", k))}</option>' for k, v in options.items())+'</select></label>'
            body += '<button name="action" value="preview">保存并查看模型检验</button>'
        else:
            body += '<p class="warn">还没有行情数据</p><div class="policy-actions"><button name="action" value="free_data">保存并准备免费日线</button><button class="secondary" name="action" value="import_data">导入数据</button></div>'
        body += '</form><details class="policy-detail"><summary>规则与数据口径</summary><p>条件规则满足时加分；必须满足的条件同时限制候选。连续涨幅与波动按当日百分位归一化。均值包含当前收盘日；决策最早下一交易日执行。</p><p>附加数值：双均线填长窗口，均值倍数填倍数，涨幅阈值用小数；其他条件为 0。空白与缺失不会算成零分。查看历史属于开发分析，不代表收益认证。</p></details>'
        body += '<script src="/static/rule-editor.js" defer></script>'
        return page("编辑经验策略", body)

    def from_form(form, old):
        doc = deepcopy(old)
        doc["name"] = str(form.get("name", doc["name"]))
        action = str(form.get("action", "preview"))
        for i, rule in enumerate(doc["rules"]):
            changed = dict(rule)
            for key in ("field", "operator"):
                if f"r{i}_{key}" in form: changed[key] = str(form[f"r{i}_{key}"])
            for key in ("window", "value", "weight"):
                if f"r{i}_{key}" in form:
                    raw = str(form[f"r{i}_{key}"])
                    changed[key] = int(raw) if key == "window" or (key == "value" and changed["operator"] == "mean_above_mean") else float(raw)
            if f"r{i}_field" in form: changed["required"] = f"r{i}_required" in form
            if any(changed[k] != rule[k] for k in ("field", "operator", "window", "value", "weight", "required")):
                changed["origin"] = "user"
            changed.pop("description", None)
            doc["rules"][i] = changed
        if action.startswith("remove_"):
            index = int(action.removeprefix("remove_"))
            if not 0 <= index < len(doc["rules"]): raise ValueError("删除规则编号无效")
            doc["rules"].pop(index)
        if action == "add":
            rule = deepcopy(rule_cards.template("volume_price_daily")["rules"][1])
            rule.update(id="r_"+uuid.uuid4().hex[:12], origin="user")
            doc["rules"].append(rule)
        return rule_cards.validate(doc), action

    @app.post("/playbooks/setup")
    async def save(req: Request):
        form = await req.form(); guard(req, form.get("token"))
        pid = str(form.get("project", ""))
        try:
            draft = projects.read(workdir, pid)
            doc, action = from_form(form, draft["rule_document"])
            previous = draft.get("source") or draft.get("rule_parent")
            series = draft.get("rule_series") or pid
            dsid = str(form.get("dataset", draft.get("dataset", "")))
            projects.update(workdir, pid, rule_document=doc, source=None, job=None, data_job=None,
                version=None, rule_parent=previous, name=doc["name"], stage="rule_editor", route="rules",
                rule_series=series, dataset=dsid, rule_build=None)
            if action == "templates": return RedirectResponse('/playbooks?project='+pid,303)
            if action == "free_data": return RedirectResponse('/train?project='+pid+'#free-data',303)
            if action == "import_data": return RedirectResponse('/data?project='+pid,303)
            if action == "add": return RedirectResponse(f'/playbooks/setup?project={pid}&edit={len(doc["rules"])-1}',303)
            if action != "preview": return RedirectResponse(f"/playbooks/setup?project={pid}", 303)
            options = available()
            if dsid not in options: raise ValueError("请选择行情数据，或先下载免费日线；草稿已保存")
            attempt = workdir/"runs/rule_attempts"/(uuid.uuid4().hex+".json")
            attempt.parent.mkdir(parents=True, exist_ok=True)
            record = {"project": pid, "series": series, "created_at": ex.now(), "status": "RUNNING", "rule_hash": ex.object_digest(doc)}
            ex.write(attempt, record)
            projects.update(workdir, pid, dataset=dsid, rule_build=attempt.stem)
            try:
                source = await run_in_threadpool(rule_signals.create, workdir, options[dsid]["path"], doc, parent=previous)
                evidence=ex.read(source/'metrics.json')
                evidence['rule_series']=series
                evidence['rule_attempts']=sum((lambda r: r.get('series',r.get('project'))==series)(ex.read(p)) for p in attempt.parent.glob('*.json'))
                if previous:
                    old=ex.read(workdir/'runs'/previous/'final_model/meta.json')['rule_card']
                    def descriptions(document):
                        return {r['id']:f"{r['description']} · {'必须满足' if r['required'] else '加分'} · 权重 {r['weight']:g}" for r in document['rules']}
                    before,after=descriptions(old),descriptions(doc)
                    evidence['rule_changes']=[{'before':before.get(key,'无'),'after':after.get(key,'已移除')}
                        for key in dict.fromkeys([*before,*after]) if before.get(key)!=after.get(key)]
                ex.write(source/'metrics.json',evidence)
                record['source']=source.name
                current=projects.read(workdir,pid)
                if current.get('rule_build')!=attempt.stem or current['rule_document']!=doc:
                    raise ValueError('草稿已变化，本次结果单独保留；请从当前草稿重新检验')
                record.update(status="SUCCEEDED", source=source.name)
                projects.update(workdir, pid, source=source.name, stage="model", version=None)
            except Exception as exc:
                record.update(status="FAILED", error=str(exc)); raise
            finally: ex.write(attempt, record)
            return RedirectResponse(f"/projects/{pid}", 303)
        except (ValueError, OSError, KeyError, TypeError) as exc: return error(exc, pid)

    @app.get("/rules/{pid}.json")
    def export(pid: str):
        doc = projects.read(workdir, pid)["rule_document"]
        return Response(rule_cards.dumps(doc), media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=rule-card.json"})

    @app.post("/rules/import")
    async def stage_import(req: Request):
        form = await req.form(); guard(req, form.get("token"))
        try:
            doc = rule_cards.loads(await form["rules"].read(rule_cards.MAX_BYTES+1), imported=True)
            pid = str(form.get("project", "")) or projects.create(workdir, route="rules", name=doc["name"])
            projects.update(workdir, pid, pending_rules=doc)
            return RedirectResponse(f"/rules/confirm?project={pid}", 303)
        except (ValueError, OSError, KeyError, TypeError) as exc: return error(exc)

    @app.get("/rules/confirm")
    def confirm_page(project: str):
        try:
            draft = projects.read(workdir, project)
            pending = rule_cards.validate(draft["pending_rules"])
        except (ValueError,OSError,KeyError,TypeError) as exc:return error(exc)
        old = draft.get("rule_document")
        body = '<h1>确认规则变更</h1><div class="rule-comparison">'
        for title, doc in (("当前规则", old), ("待确认规则", pending)):
            body += '<section><h2>'+title+'</h2><ul>' + (''.join(f'<li>{esc(r["description"])} · {"必须满足" if r["required"] else "加分"} · 权重 {r["weight"]:g} · {esc(r["origin"])}</li>' for r in doc['rules']) if doc else '<li>尚无规则</li>')+'</ul></section>'
        body += '</div><form method="post" action="/rules/confirm">'+hidden(project)+f'<input type="hidden" name="pending_hash" value="{ex.object_digest(pending)}"><button>确认并建立规则草稿</button></form><p><a href="/playbooks/setup?project='+project+'">返回当前规则</a></p>'
        return page("确认导入", body)

    @app.post("/rules/confirm")
    async def confirm(req: Request):
        form = await req.form(); guard(req, form.get("token"))
        try:
            pid = str(form["project"])
            draft = projects.read(workdir, pid)
            doc = rule_cards.validate(draft["pending_rules"])
            if ex.object_digest(doc) != str(form.get("pending_hash")): raise ValueError("预览已变化，请重新查看并确认")
            projects.update(workdir, pid, rule_document=doc, pending_rules=None, source=None,
                rule_parent=draft.get("source") or draft.get("rule_parent"), version=None, job=None, data_job=None,
                stage="rule_editor", route="rules", name=doc["name"])
            return RedirectResponse(f"/playbooks/setup?project={pid}", 303)
        except (ValueError, OSError, KeyError, TypeError) as exc: return error(exc)
