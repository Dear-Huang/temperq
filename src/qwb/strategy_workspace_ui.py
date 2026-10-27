"""Progressive policy editor and daily dashboard for frozen strategy versions."""
from __future__ import annotations

from dataclasses import asdict
import html
import json
from pathlib import Path

import pandas as pd
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response, JSONResponse

from . import experiments as ex, policy, strategy, strategy_workspace as ws, experience_ui, starter_models, external_signals, policy_templates, strategy_projects as projects, experience_playbooks, rule_signals, rule_cards, scorecard_export
from .governance_ui import REASONS
from .strategy_ui import reason_text


def esc(value):
    return html.escape(str(value), quote=True)


def register(app, workdir, *, page, guard, token, locate, submit, datasets):
    training_locate=locate
    def locate(source):
        if source.startswith('rule_'):
            if not ex.ID_RE.fullmatch(source[5:]):raise ValueError('规则信号编号无效')
            root=workdir/'runs'/source;rule_signals.validate_model(ex.read(root/'final_model/meta.json'));return root
        if source.startswith('starter_'):
            if source not in {'starter_'+key for key in starter_models.catalog()}:raise ValueError('未知入门模型')
            root=workdir/'runs'/source;starter_models.verify(root);return root
        if source.startswith('external_'):
            if not ex.ID_RE.fullmatch(source.removeprefix('external_')):raise ValueError('外部信号编号无效')
            root=workdir/'runs'/source;external_signals.validate_meta(ex.read(root/'final_model/meta.json'));return root
        return training_locate(source)
    def hidden():
        return f'<input type="hidden" name="token" value="{token}">'

    def error(exc):
        response=page("需要调整",f'<h1>这一步没有完成</h1><p class="box bad">{esc(exc)}</p>'
                      '<p><a href="/start">返回我的策略</a> · <a href="/data">检查数据</a> · <a href="javascript:history.back()">返回修改</a></p>')
        response.status_code=400
        return response

    def header(step, base="",project=""):
        return '<p><a href="/start">← 我的策略</a></p>'+experience_ui.flow(step, base,project)

    def get(sid,vid):
        try:return ws.inspect(workdir,sid,vid)
        except (ValueError,OSError,KeyError) as exc:raise HTTPException(409,str(exc)) from exc

    def model_result(source, dataset, *, project="", base=""):
        root=locate(source) if not base else ws.revision(workdir,*base.split('/')[-2:],verify_implementation=False)[0]
        meta=ex.read(root/'final_model/meta.json')
        feed=datasets(workdir)[dataset]['path'] if not base else get(*base.split('/')[-2:])['spec']['data_feed']
        if feed is None:
            m=ex.read(root/'metrics.json')
            body=header(1)+'<h1>模型检验</h1><p class="warn">合成数据仅用于训练演示；进入账户规则前，请准备持续行情并重新训练。当前训练结果已保留。</p>'
            body+=experience_ui.signal_report(m)
            return page('模型检验',body+'<a class="btn primary" href="/train'+('?project='+project if project else '')+'#free-data">准备真实行情</a>')
        from . import daily_data
        if not (daily_data.current(feed)/'calendar.csv').is_file():
            raise ValueError('行情缺少交易日历，尚不能进入规则阶段。请在数据页补充供应方的 date、is_open 日历，或改用带日历的免费行情；训练结果已保留。')
        # Use the same data, calendar and prediction contract as freezing a rule.
        _,panel,_,_,_,_=ws.inputs(workdir,root,{'data_feed':str(feed)})
        ws.history(root,panel)
        body=header(1,base,project)+'<h1>模型检验</h1>'
        model_label=meta.get('name','经验打分卡') if meta['model']=='rule_card' else Path(source).name
        body+=f'<p class="policy-meta">{esc(model_label)} · 数据截止 {esc(meta["data_end"])}</p>'
        body+='<p>数据与预测输出检查已完成</p>'
        metrics=root/('model-evaluation.json' if base else 'metrics.json')
        if metrics.is_file():
            m=ex.read(metrics)
            if m.get('leak_check',{}).get('ok') is False:
                raise ValueError('模型前视检查未通过，请修正数据或训练设置后重新训练')
            body+=experience_ui.signal_report(m)
        else:
            body+='<p class="mut">排序能力暂不能判断：此来源没有本平台的模型检验报告。输出检查不认证上游无前视，也不代表收益优势。</p>'
        target=f'/projects/{project}/rules' if project else base+'/edit'
        body+=f'<p><a class="btn primary" href="{target}">下一步：设置规则 →</a></p>'
        if project:
            if meta['model']=='rule_card':
                body+=f'<p><a href="/playbooks/setup?project={project}">修改经验规则</a> · <a href="/strategies/new?project={project}">换一个模型</a></p>'
                body+=f'<p><a href="/rules/{project}.json">导出规则卡</a></p>'
            else:
                body+=f'<p><a href="/train?project={project}">重新训练</a> · <a href="/strategies/new?project={project}">换一个模型</a></p>'
        else:
            body+=f'<form method="post" action="{base}/change-model">'+hidden()+'<button class="secondary">更换模型（新版本）</button></form>'
        return page('模型检验',body)

    @app.post('/projects')
    async def begin(req:Request):
        form=await req.form();guard(req,form.get('token'))
        route=str(form.get('route','train'))
        if route not in {'train','select'}:return error('未知的开始方式')
        pid=projects.create(workdir,route=route)
        return RedirectResponse('/train?project='+pid if route=='train' else '/strategies/new?project='+pid,303)

    @app.get('/projects/{pid}')
    def project(pid:str):
        try:
            draft=projects.resolved(workdir,pid)
            if draft.get('pending_rules'):
                return RedirectResponse('/rules/confirm?project='+pid,303)
            if draft.get('version'):
                state=get(*draft['version'].split('/'))
                suffix='/dashboard' if state['runtime'] else '/deploy' if any(r['status']=='SUCCEEDED' for r in state['evaluations']) else ''
                return RedirectResponse('/strategies/'+draft['version']+suffix,303)
            if draft.get('data_job'):
                if draft.get('data_job_status')!='succeeded':return RedirectResponse('/job/'+draft['data_job'],303)
                if draft.get('route')=='rules':return RedirectResponse('/playbooks/setup?project='+pid,303)
                target='/strategies/new?project='+pid+'&source='+draft['next_signal']+'&dataset='+draft['dataset'] if draft.get('next_signal') else '/train?project='+pid+'&ds='+draft['dataset']
                return RedirectResponse(target,303)
            if draft.get('route')=='rules' and not draft.get('source'):
                return RedirectResponse('/playbooks/setup?project='+pid,303)
            if draft.get('job') and draft.get('job_status')!='succeeded':return RedirectResponse('/job/'+draft['job'],303)
            if draft.get('source') and draft.get('dataset'):
                if draft.get('stage')=='rules':return RedirectResponse('/projects/'+pid+'/rules',303)
                return model_result(draft['source'],draft['dataset'],project=pid)
            return RedirectResponse('/train?project='+pid if draft['route']=='train' else '/strategies/new?project='+pid,303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.get('/projects/{pid}/model')
    def project_model(pid:str):
        try:
            draft=projects.resolved(workdir,pid)
            if draft.get('route')=='rules' and not draft.get('source'):
                return RedirectResponse('/playbooks/setup?project='+pid,303)
            return model_result(draft['source'],draft['dataset'],project=pid)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.post('/projects/{pid}/select')
    async def select_model(req:Request,pid:str):
        form=await req.form();guard(req,form.get('token'))
        try:
            projects.read(workdir,pid)
            source,dataset=str(form['source']),str(form['dataset'])
            # Do not claim completion before the output contract is checked.
            model_result(source,dataset,project=pid)
            projects.update(workdir,pid,source=source,dataset=dataset,job=None,data_job=None,stage='model')
            return RedirectResponse('/projects/'+pid,303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.post('/projects/select')
    async def select_new(req:Request):
        form=await req.form();guard(req,form.get('token'))
        try:
            source,dataset=str(form['source']),str(form['dataset'])
            model_result(source,dataset)
            pid=projects.create(workdir,route='select')
            projects.update(workdir,pid,source=source,dataset=dataset,template=str(form.get('template','')))
            return RedirectResponse('/projects/'+pid,303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.get('/projects/{pid}/rules')
    def project_rules(pid:str):
        try:
            draft=projects.resolved(workdir,pid)
            if draft.get('version'):return project(pid)
            if datasets(workdir)[draft['dataset']]['path'] is None:raise ValueError('合成数据仅用于训练演示，请准备持续行情并重新训练；原结果已保留')
            model_result(draft['source'],draft['dataset'],project=pid)
            previous=get(draft['sid'],draft['parent']) if draft.get('parent') else None
            if draft.get('stage')!='rules':projects.update(workdir,pid,stage='rules')
            return editor(draft['source'],draft['dataset'],previous,template=draft.get('template',''),project=pid,replace_model=True)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.get('/strategies/{sid}/{vid}/model')
    def frozen_model(sid:str,vid:str):
        try:
            state=get(sid,vid)
            return model_result(state['spec']['signal']['source'],state['spec']['data_feed'],base=f'/strategies/{sid}/{vid}')
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.post('/strategies/{sid}/{vid}/change-model')
    async def change_model(req:Request,sid:str,vid:str):
        form=await req.form();guard(req,form.get('token'))
        state=get(sid,vid)
        pid=projects.create(workdir,route='select',name=state['spec']['name'],sid=sid,parent=vid)
        return RedirectResponse('/strategies/new?project='+pid,303)

    @app.get("/strategies")
    def index():
        body='<div class="page-heading"><h1>我的策略</h1><a class="btn" href="/strategies/new">新建策略</a></div>'
        for identity in sorted((workdir/"runs/strategies").glob("*/identity.json"),key=lambda p:p.stat().st_mtime,reverse=True):
            family=ex.read(identity)
            body+=f'<section class="box"><h2>{esc(family["name"])}</h2>'
            for path in reversed(ws.versions(workdir,identity.parent.name)):
                spec=ex.read(path)
                runtime=ex.read(path.parent/"runtime.json") if (path.parent/"runtime.json").exists() else None
                health=ex.read(path.parent/"health.json") if (path.parent/"health.json").exists() else {}
                route=f'/strategies/{spec["sid"]}/{spec["vid"]}'+('/dashboard' if runtime else '')
                body+=f'<p><a href="{route}">版本 {spec["vid"][:8]}</a> · {esc(spec["name"])} · '
                body+=esc(health.get("message","待检验 / 待启用"))
                if runtime:body+=' · '+("历史演示" if runtime["scope"]=="historical_replay" else "纸面观察")
                body+='</p>'
            body+='</section>'
        return page("我的策略",body)

    @app.get("/strategies/new")
    def new(source:str="",dataset:str="",template:str="",project:str=""):
        if project:
            try:projects.read(workdir,project)
            except (ValueError,OSError,KeyError) as exc:return error(exc)
        if template:
            try:policy_templates.read(workdir,template)
            except (ValueError,OSError,KeyError) as exc:return error(exc)
        sources=experience_ui.signal_options(workdir)
        options=''.join(f'<option value="{esc(candidate)}" {"selected" if candidate==source else ""}>{esc(meta["name"])} · {esc({"lightgbm":"LightGBM","ridge":"Ridge","external_scores":"外部预测"}.get(meta["model"],meta["model"]))} · {esc(meta["data_end"])}</option>' for candidate,meta in sources.items())
        available={k:v for k,v in datasets(workdir).items() if k!="_trial"}
        data_options=''.join(f'<option value="{esc(k)}" {"selected" if k==dataset else ""}>{esc(k)}'+(f' · {esc(v["snap"]["start"])} 至 {esc(v["snap"]["end"])}' if "snap" in v else '')+'</option>' for k,v in available.items())
        context='?project='+project if project else ''
        body=header(1)+'<h1>选择模型</h1><div class="policy-actions"><a href="/train'+context+'">训练新模型</a><a href="/signals/import'+context+'">导入外部预测</a><a href="/policy-templates">导入规则模板</a></div>'
        if project and projects.read(workdir,project).get('parent'):body+='<p class="warn">更换模型将继承原规则；新版本需要重新检验和部署，旧账户继续保留。</p>'
        if template:body+='<p class="box">已载入分享模板。先选自己的信号和行情，下一步会显示模板规则；模板不包含账户或成绩。</p>'
        if not sources or not available:
            missing='兼容的五日模型' if not sources else '持续行情数据源'
            starter_selected=source in {'starter_'+key for key in starter_models.catalog()}
            href='/train?next_signal='+source+('&project='+project if project else '')+'#free-data' if not available and starter_selected else '/train'+context+('#free-data' if not available else '')
            action='准备行情，继续使用已选模型' if not available and starter_selected else ('准备数据并训练' if not available else '训练一个五日模型')
            body+=f'<section class="policy-next"><h2>需要{missing}</h2><a class="btn primary" href="{esc(href)}">{action}</a></section>'
        for key,card in starter_models.catalog().items():
            body+=f'<details class="starter-choice"{" open" if not sources else ""}><summary>{esc(card["name"])}</summary><p class="mut">40 股入门样本 · 非历史全市场 · 行情另行下载</p><form action="/strategies/starter" method="post">'+hidden()+f'<input type="hidden" name="project" value="{project}"><input type="hidden" name="model_id" value="{esc(key)}"><button>使用入门模型</button></form><p><a href="/starter-models/{esc(key)}.json">模型卡与复现配方</a></p></details>'
        body+=f'<form action="/projects/{project}/select" method="post">'+hidden() if project else '<form action="/projects/select" method="post">'+hidden()
        if template:body+=f'<input type="hidden" name="template" value="{template}">'
        body+=f'<label class="f">模型<select name="source" required {"disabled" if not sources else ""}>{options or "<option>还没有兼容模型</option>"}</select></label>'
        body+=f'<label class="f">行情数据<select name="dataset" required {"disabled" if not available else ""}>{data_options or "<option>还没有行情数据</option>"}</select></label>'
        body+=f'<div class="policy-actions"><button {"disabled" if not sources or not available else ""}>下一步</button></div></form>'
        return page("选择信号",body)

    @app.get('/starter-models/{model_id}.json')
    def starter_card(model_id:str):
        if model_id not in starter_models.catalog():raise HTTPException(404)
        return JSONResponse(starter_models.catalog()[model_id])

    @app.post('/strategies/starter')
    async def starter(req:Request):
        form=await req.form();guard(req,form.get('token'))
        try:
            source=starter_models.install(workdir,str(form['model_id']))
            project=str(form.get('project',''))
            if project:projects.read(workdir,project)
            return RedirectResponse('/strategies/new?source='+source.name+'&dataset=free_daily'+('&project='+project if project else ''),303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    def editor(source,dataset,previous=None,template="",project="",replace_model=False):
        old=previous["spec"] if previous else None
        draft = projects.read(workdir, project) if project else {}
        capital=ws.capital_context(ws.location(workdir,old["sid"])/"versions"/old["vid"] if old and not replace_model else locate(source),
            old["data_feed"] if old and not replace_model else datasets(workdir)[dataset]["path"],
            old["cash"] if old else 100000,old["holdings"] if old else [])
        resolved=old["resolved"] if old else (experience_playbooks.resolve(draft["playbook"], capital=capital)
                                              if draft.get("playbook") else policy.resolve("direct",capital=capital))
        if template:
            imported,origin=policy_templates.read(workdir,template)
            resolved=policy.resolve(imported['preset'],imported['policy']['parameters'],imported['policy']['mode'],
                capital=capital,cost_model=imported['policy']['cost_model'])
        chosen=resolved["policy"]; values=chosen["parameters"]
        body=header(2,f'/strategies/{old["sid"]}/{old["vid"]}' if old else '',project)+'<h1>账户规则</h1>'
        body+='<form method="post" action="/strategies" enctype="multipart/form-data">'+hidden()
        if project:body+=f'<input type="hidden" name="project" value="{project}">'
        if replace_model:body+='<input type="hidden" name="replace_model" value="yes">'
        if template:body+=f'<input type="hidden" name="policy_template" value="{template}"><p class="box">正在使用导入的完整规则；模板参数按用户明确选择保留，资金变化不会自动覆盖。可微调，或恢复预设以重新采用资金默认值。</p>'
        body+=f'<input type="hidden" name="source" value="{esc(source)}"><input type="hidden" name="dataset" value="{esc(dataset)}">'
        if project and draft.get('playbook'):
            body+=f'<input type="hidden" name="playbook" value="{esc(draft["playbook"])}"><input type="hidden" name="experience_notes" value="{esc(draft.get("experience_notes", ""))}"><p class="box">经验模板：{esc(experience_playbooks.get(draft["playbook"]).name)}。规则卡会作为第一版参考；你可以在高级设置中逐项修改。</p>'
        if old:body+=f'<input type="hidden" name="sid" value="{old["sid"]}"><input type="hidden" name="parent" value="{old["vid"]}">'
        body+=f'<label class="f">策略名称<input name="name" maxlength="80" value="{esc(old["name"] if old else projects.read(workdir,project)["name"] if project else "我的策略")}" required></label>'
        body+=f'<label class="f">可用现金（元）<input name="cash" type="number" min="0" step="0.01" value="{old["cash"] if old else 100000}" required></label>'
        body+='<p class="mut" id="capital-default-note" role="status"></p><input type="hidden" name="explicit_overrides" id="explicit-overrides">'
        body+='<label class="f">规则模板<select name="preset" id="policy-preset">'+''.join(
            f'<option value="{k}" {"selected" if k==resolved["preset"] else ""}>{esc(v["name"])}</option>' for k,v in policy.PRESETS.items())+'</select></label>'
        body+='<section class="policy-snapshot" aria-label="规则摘要"><h2>配置摘要</h2><p id="policy-summary-text" aria-live="polite">正在读取设置</p><details class="policy-changes"><summary id="policy-change-count">我的改动</summary><ul id="policy-change-list"></ul></details></section>'
        body+='<p class="warn" id="policy-cost-warning" hidden>仅计佣金税费，成本不完整。</p>'
        body+='<details id="advanced"><summary>高级设置</summary><button type="button" class="secondary" id="reset-policy">恢复模板默认值</button>'
        body+='<div class="policy-advanced-tools"><label>查找参数 <input id="policy-search" type="search" placeholder="例如：现金 / n_hold"></label><small id="policy-search-result" role="status"></small></div>'
        body+='<label class="f">风险控制<select name="mode" id="policy-mode">'+''.join(
            f'<option value="{k}" {"selected" if k==chosen["mode"] else ""}>{v}</option>' for k,v in
            (("fixed","固定仓位"),("ewma","EWMA 波动目标"),("ml","ML 风险辅助 · 实验")))+'</select></label>'
        body+='<label class="f">成本口径<select name="cost_model" id="policy-cost-model">'+''.join(
            f'<option value="{key}" {"selected" if key==chosen.get("cost_model","commission_only") else ""}>{label}</option>' for key,label in
            (("struct_v1","STRUCT 半价差估计 + 佣金税费"),("commission_only","仅佣金税费 · 成本不完整")))+'</select></label>'
        ui_schema=policy.schema()
        definition=next(v for v in ui_schema["$defs"].values() if "n_hold" in v.get("properties",{}))
        groups=(("选股与保留",("n_hold","hold_until_rank")),("仓位与现金",("max_weight","exposure_cap","cash_floor")),("风险与调整",("adjustment_speed","target_volatility")),("实际费用与压力",("commission_bp","minimum_commission","slippage_bp")))
        for title,keys in groups:
            body+=f'<details class="policy-group" open><summary>{title}</summary>'
            for key in keys:
                field=definition["properties"][key];info=ui_schema["ui"][key]
                bounds=''.join(f' {attr}="{field[k]}"' for k,attr in (("minimum","min"),("exclusiveMinimum","min"),("maximum","max")) if k in field)
                equivalent='<small class="field-equivalent"></small>' if info["unit"]=="比例" else ''
                body+=f'<label class="f" data-field="{key}">{esc(info["label"])}（{esc(info["unit"])}）<input name="{key}" type="number" step="{"1" if field["type"]=="integer" else "any"}" value="{values[key]}"{bounds} required>{equivalent}</label>'
            body+='</details>'
        body+='<label class="f">现有持仓 CSV（可选）<input type="file" name="holdings" accept=".csv"></label><p><a href="/account-template.csv">下载持仓模板</a></p>'
        if old and old["holdings"]:body+='<p>未上传新文件时保留旧版本的初始持仓。</p>'
        body+='</details><div class="policy-actions"><button>保存并继续</button></div></form>'
        presets={k:policy.resolve(k,capital=capital)["policy"]|{"description":v["description"],"name":v["name"]} for k,v in policy.PRESETS.items()}
        data=json.dumps(presets,ensure_ascii=False).replace("<","\\u003c")
        body+='<script type="application/json" id="policy-presets">'+data+'</script>'
        pins=[k for k,v in resolved["provenance"].items() if v=="user"]
        body+='<script type="application/json" id="policy-capital">'+json.dumps({"pinned":pins,"capital":capital},ensure_ascii=False).replace("<","\\u003c")+'</script>'
        body+='<dialog id="preset-change-dialog" class="preset-dialog" aria-labelledby="preset-change-title"><h2 id="preset-change-title">载入另一套预设？</h2><p>当前微调还未冻结。载入会替换政策参数，策略名称、现金和已选的持仓文件保留。</p><div class="actions"><button type="button" class="secondary" id="keep-policy">保留当前微调</button><button type="button" id="replace-policy">切换并载入默认</button></div></dialog>'
        return page("账户规则",body)

    @app.get("/strategies/configure")
    def configure(source:str,dataset:str,template:str=""):
        try:
            locate(source);datasets(workdir)[dataset]
            return editor(source,dataset,template=template)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.get("/strategies/{sid}/{vid}/edit")
    def edit(sid:str,vid:str):
        state=get(sid,vid)
        return editor(state["spec"]["signal"]["source"],state["spec"]["data_feed"],state)

    async def form_context(form):
        parent=str(form.get("parent",""));sid=str(form.get("sid","")) or None
        old=ws.revision(workdir,sid,parent,verify_implementation=False) if parent and sid else None
        source=old[0] if old and not form.get('replace_model') else locate(str(form["source"]))
        feed=old[1]["data_feed"] if old and not form.get('replace_model') else datasets(workdir)[str(form["dataset"])]["path"]
        uploaded=form.get("holdings");holdings=old[1]["holdings"] if old else []
        if uploaded and getattr(uploaded,"filename",""):
            holdings=strategy.read_holdings(await uploaded.read(2_000_001))
        return source,feed,holdings,sid

    @app.post("/strategies/defaults")
    async def defaults(req:Request):
        form=await req.form();guard(req,form.get("token"))
        try:
            source,feed,holdings,_=await form_context(form)
            capital=ws.capital_context(source,feed,float(form["cash"]),holdings)
            return JSONResponse(policy.resolve(str(form["preset"]),capital=capital))
        except (ValueError,OSError,KeyError) as exc:return JSONResponse({"error":str(exc)},status_code=400)

    @app.post("/strategies")
    async def create(req:Request):
        form=await req.form();guard(req,form.get("token"))
        try:
            project=str(form.get('project',''))
            if project:
                draft=projects.resolved(workdir,project)
                if draft.get('version'):return RedirectResponse('/strategies/'+draft['version'],303)
                if str(form.get('source'))!=draft.get('source') or str(form.get('dataset'))!=draft.get('dataset'):
                    raise ValueError('模型选择已变化，请重新打开规则设置')
                if datasets(workdir)[draft['dataset']]['path'] is None:raise ValueError('合成数据仅用于训练演示，请准备持续行情')
                model_result(draft['source'],draft['dataset'],project=project)
            source,feed,holdings,sid=await form_context(form)
            playbook_id=str(form.get('playbook',''))
            preset=str(form["preset"])
            if playbook_id:
                preset=experience_playbooks.get(playbook_id).preset
            base=policy.resolve(preset)["policy"]["parameters"]
            overrides={}
            explicit=json.loads(str(form["explicit_overrides"])) if form.get("explicit_overrides") else None
            if explicit is not None and (not isinstance(explicit,list) or any(k not in base for k in explicit)):
                raise ValueError("参数修改清单无效")
            for k,v in base.items():
                if k not in form:continue
                x=int(form[k]) if type(v) is int else float(form[k])
                if explicit is None or k in explicit:overrides[k]=x
            sid,vid=ws.create(workdir,source,feed,name=str(form["name"]),cash=float(form["cash"]),
                preset=preset,overrides=overrides,mode=str(form["mode"]),holdings=holdings,sid=sid,
                cost_model=str(form.get("cost_model","struct_v1")),
                template_origin=("experience_playbook:"+playbook_id if playbook_id else
                                 policy_templates.read(workdir,str(form['policy_template']))[1] if form.get('policy_template') else None),
                experience_notes=str(form.get('experience_notes','')), playbook_id=playbook_id)
            for name in ('metrics.json','model-evaluation.json'):
                if (source/name).is_file():
                    ex.write(ws.location(workdir,sid)/'versions'/vid/'model-evaluation.json',ex.read(source/name))
                    break
            if project:projects.update(workdir,project,version=sid+'/'+vid,name=str(form['name']))
            return RedirectResponse(f"/strategies/{sid}/{vid}",303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.get("/strategies/{sid}/{vid}/rules")
    @app.get("/strategies/{sid}/{vid}")
    def dashboard(sid:str,vid:str):
        state=get(sid,vid);spec=state["spec"];base=f'/strategies/{sid}/{vid}'
        results=state["evaluations"];success=[r for r in results if r["status"]=="SUCCEEDED"]
        latest=success[-1] if success else None
        mode_name={"fixed":"固定仓位","ewma":"EWMA 波动目标","ml":"ML 风险辅助 · 实验"}[spec["resolved"]["policy"]["mode"]]
        body=header(2,base)+f'<h1>{esc(spec["name"])}</h1><p class="policy-meta">规则检验 · 版本 {vid[:8]} · {esc(policy.PRESETS[spec["resolved"]["preset"]]["name"])} · {mode_name}'
        body+=f' · <a href="{base}/edit">修改并建立新版本</a></p>'
        if spec['signal'].get('kind')=='rule_card':
            body+='<form action="/rules/revise" method="post">'+hidden()+f'<input type="hidden" name="sid" value="{sid}"><input type="hidden" name="vid" value="{vid}"><button class="secondary">修改经验规则（新版本）</button></form>'
        if state["runtime"]:
            body+=f'<section class="policy-next"><h2>纸面运行</h2><p>{esc(state["health"]["message"])}</p><a class="btn primary" href="{base}/dashboard">打开部署看板</a> <a href="#running">运行设置</a></section>'
        elif latest:
            body+=f'<section class="policy-next" id="activation"><h2>规则检验完成</h2><a class="btn primary" href="{base}/deploy">下一步：部署纸面账户 →</a></section>'
        body+='<div class="policy-summary-status"><span>可运行性：<strong>'
        operability=latest.get("operability") if latest else None
        body+=esc(operability['message']) if operability else '待检验'
        body+='</strong></span><span>经济增量：<strong>'+('证据不足' if latest else '未检验')+'</strong></span></div>'
        body+=f'<p class="policy-meta">{len(results)} 次检验 · {len(ws.versions(workdir,sid))} 个版本</p>'
        if spec['signal'].get('kind')=='rule_card':
            dates=pd.Series(ex.read(ws.location(workdir,sid)/'versions'/vid/'final_model/signal_dates.json')['dates'])
        else:
            predictions=pd.read_parquet(ws.location(workdir,sid)/"versions"/vid/"predictions.parquet",columns=["decision_time"])
            dates=pd.to_datetime(predictions.decision_time,utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
        body+='<details id="history"><summary>历史检验与对照（展开）</summary>' if state["runtime"] else '<section id="history">'
        if not state["runtime"]:
            body+='<h2>规则检验</h2>'
            if latest:body+='<details><summary>重新检验</summary>'
            body+=f'<form method="post" action="{base}/evaluate">'+hidden()
            body+=f'<label class="f">开始<input type="date" name="start" value="{esc(dates.min())}" required></label>'
            body+=f'<label class="f">结束<input type="date" name="end" value="{esc(dates.max())}" required></label><button>开始检验</button></form>'
            if latest:body+='</details>'
        if latest:
            labels=[("selected","当前政策"),("immediate","立即调整")]
            if "exposure_matched" in latest["metrics"]:labels.append(("exposure_matched","平均仓位匹配 · 固定仓位立即调整"))
            body+='<h2>历史对照</h2><p>蓝色：当前政策 · 灰色：立即调整 · 橙色：平均仓位匹配（如有）</p>'
            root,_=ws.revision(workdir,sid,vid,verify_implementation=False)
            dirs=[p.parent for p in sorted(root.glob("evaluations/*/result.json"),key=lambda p:p.stat().st_mtime) if ex.read(p)["status"]=="SUCCEEDED"]
            arms=[ex.read(dirs[-1]/(k+".json")) for k,_ in labels]
            values=[[row["equity"]/arm["path"][0]["equity"] for row in arm["path"]] for arm in arms]
            lo,hi=min(min(v) for v in values),max(max(v) for v in values);span=max(hi-lo,.001)
            plot_hi=lo+span;precision=4 if span<.01 else 3
            lines=''
            for v,color in zip(values,("#2984dd","#8c929c","#d68124")):
                points=' '.join(f'{30+i*880/max(1,len(v)-1):.2f},{210-(x-lo)*180/span:.2f}' for i,x in enumerate(v))
                lines+=f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>'
            axes=''.join(f'<line x1="30" x2="910" y1="{y}" y2="{y}" stroke="#8c929c" opacity=".2"/>'
                f'<text x="32" y="{y-5}" fill="#737d8b" font-size="12">{value:.{precision}f}</text>'
                for value,y in ((lo,210),((plot_hi+lo)/2,120),(plot_hi,30)))
            axes+=f'<text x="30" y="235" fill="#737d8b" font-size="12">{esc(latest["start"])}</text><text x="910" y="235" text-anchor="end" fill="#737d8b" font-size="12">{esc(latest["end"])}</text>'
            body+='<svg class="policy-chart" viewBox="0 0 940 250" role="img" aria-label="历史归一净值对照">'+axes+lines+'</svg>'
            body+='<div class="tw"><table><tr><th>政策</th><th>收益</th><th>最大回撤</th><th>平均实际仓位</th><th>已计费用（元）</th><th>交易额 / 权益累计</th></tr>'
            for key,label in labels:
                m=latest["metrics"][key]
                body+=f'<tr><td>{label}</td><td>{m["total_return"]:.2%}</td><td>{m["max_drawdown"]:.2%}</td><td>{m["mean_exposure"]:.2%}</td><td>{m["total_cost"]:.2f}</td><td>{m["gross_traded_fraction"]:.2f}</td></tr>'
            body+='</table></div>'+f'<p class="policy-meta">{latest["metrics"]["selected"]["sessions"]} 段日收益 · 尚未证明未来收益优势</p>'
            match=latest.get("exposure_match")
            if match and match['status']!='MATCHED_WITHIN_TOLERANCE':
                body+='<p class="warn">平均仓位未匹配成功，不能据此归因。</p>'
            body+='<details class="policy-detail"><summary>检验口径与费用差异</summary>'+f'<p>{esc(latest["message"])}</p>'
            difference=latest["cost_difference"]
            body+=f'<p>这段路径已计费用比立即调整{"多" if difference>0 else "少"} {abs(difference):.2f} 元；请同时比较平均实际仓位。费用差包含交易规模和持仓路径变化，不代表全部成本或收益增量。</p>'
            if match:
                body+=f'<p>平均实际仓位匹配：{"已进入容差" if match["status"]=="MATCHED_WITHIN_TOLERANCE" else "未匹配成功，不据此归因"}；误差 {match["signed_match_error"]:.2%}，容差 ±{match["tolerance"]:.2%}。使用全段历史校准，仅用于解释，不是可运行政策，也不是收益优势证明。</p>'
            phases=latest.get("phases")
            if phases:
                body+=f'<details><summary>建仓观察期与后段分开看</summary><p>预先规定前 {phases["startup_sessions"]} 段日收益为建仓观察期；按调整速度计算，不根据收益挑日期。后段仍不保证达到稳态。短区间可能没有后段样本。</p>'
                body+='<div class="tw"><table><tr><th>组别</th><th>阶段</th><th>日收益段数</th><th>收益</th><th>回撤</th><th>平均仓位</th><th>费用（元）</th></tr>'
                for key,label in labels:
                    for phase,title in (("startup","建仓观察期"),("post_startup","后段")):
                        m=phases[key][phase]
                        body+=f'<tr><td>{label}</td><td>{title}</td><td>{m["sessions"]}</td>'
                        body+=(f'<td>{m["total_return"]:.2%}</td><td>{m["max_drawdown"]:.2%}</td><td>{m["mean_exposure"]:.2%}</td><td>{m["total_cost"]:.2f}</td>' if m["sessions"] else '<td colspan="4">无样本，不能比较</td>')+'</tr>'
                body+='</table></div></details>'
            body+='</details>'
        body+='</details>' if state["runtime"] else '</section>'
        failures=[r for r in results if r["status"]!="SUCCEEDED"]
        if failures:body+='<details><summary>未完成的尝试</summary>'+''.join(f'<p>{esc(r["status"])} · {esc(r.get("error","运行中"))}</p>' for r in failures)+'</details>'
        if state["runtime"]:
            runtime=state["runtime"];records=state["records"]
            counts={scope:sum(r["scope"]==scope for r in records) for scope in ("forward_paper","late_reconstruction","historical_replay")}
            body+='<h2 id="running">日常运行</h2>'+f'<p>启用日 {esc(runtime["start_date"])} · 按时纸面记录 {counts["forward_paper"]} 天 · 迟到重建 {counts["late_reconstruction"]} 天 · 历史演示 {counts["historical_replay"]} 天</p>'
            if spec['signal'].get('kind')=='external_scores':
                external=ex.read(ws.location(workdir,sid)/'versions'/vid/'final_model/meta.json')
                body+=f'<p><a href="/signals/{esc(external["run_id"])}">追加外部预测</a>，然后检查并记账；缺失信号不会用昨日排名代替。源端生成时刻为自报，首次接收时刻由本平台记录。</p>'
            body+=f'<form method="post" action="{base}/tick">'+hidden()+'<button>检查新行情并记账</button></form>'
            body+=f'<form method="post" action="{base}/automation">'+hidden()+f'<input type="hidden" name="enabled" value="{"no" if runtime["enabled"] else "yes"}"><button class="secondary">{"关闭" if runtime["enabled"] else "开启"}每日检查</button></form>'
            if records:
                last=records[-1]
                body+=f'<p>最后记录 {esc(last["day"])} · 权益 {last["path"]["equity"]:.2f} 元 · 现金 {last["path"]["cash"]:.2f} 元</p>'
                if last["constraint_violations"]:
                    body+=f'<p class="warn">实际持仓存在 {len(last["constraint_violations"])} 项约束偏离，详情在导出的运行记录和决策页中保留。</p>'
                pending=next((r["decision"] for r in reversed(records) if r.get("decision")),None)
                if pending:body+=f'<p>最近建议日 {esc(pending["date"])} → 计划执行日 {esc(pending["execution_date"])} · 目标股票仓位 {pending["target_exposure"]:.1%}。详见决策回放。</p>'
        body+=f'<p><a href="{base}/decisions">逐日决策与成交</a> · <a href="{base}/config.json">导出冻结配置</a> · <a href="{base}/records.csv">导出运行记录 CSV</a></p>'
        body+=f'<p><a href="{base}/scorecard.json">下载成绩卡 JSON</a> · <a href="{base}/scorecard.md">下载成绩卡 Markdown</a></p>'
        body+='<details><summary>版本历史</summary>'
        for path in reversed(ws.versions(workdir,sid)):
            version=ex.read(path);running=(path.parent/'runtime.json').is_file()
            body+=f'<p><a href="/strategies/{sid}/{version["vid"]}'+('/dashboard' if running else '')+f'">{esc(version["name"])} · {version["vid"][:8]}</a> · '+('账户保留' if running else '独立检验与部署')+'</p>'
        body+='</details>'
        body+=f'<details><summary>把这套规则分享给别人</summary><p>分享模板只包含政策参数、期限和版本，不含现金、持仓、账户路径、模型文件或成绩。对方须使用自己的信号和账户重新检验。</p><a href="{base}/policy-template.json">下载可分享政策模板</a> · <a href="/policy-templates">导入政策模板</a></details>'
        body+='<details><summary>实际生效值、修改与来源</summary><p>以下冻结配置同时用于检验和运行。固定模式的 target_volatility 不生效。</p>'
        body+=f'<pre class="plain">{esc(json.dumps(spec["resolved"],ensure_ascii=False,indent=2))}</pre>'
        body+=f'<p>配置哈希 <code>{esc(spec["spec_hash"])}</code></p></details>'
        cost_name='STRUCT 上月报价半价差估计、佣金、按日印花税及额外滑点压力' if spec['resolved']['policy'].get('cost_model')=='struct_v1' else '佣金、按日印花税及自填滑点压力（未计报价价差）'
        body+=f'<details class="policy-detail"><summary>本版本成本</summary><p>{cost_name}。实际成交冲击未标定。</p></details>'
        body+=f'<details><summary>高级：调仓时机试验</summary><a href="{base}/timing">比较固定周期与周期外调整</a></details>'
        return page(spec["name"],body)

    @app.get('/strategies/{sid}/{vid}/deploy')
    def deploy(sid:str,vid:str):
        state=get(sid,vid);spec=state['spec'];base=f'/strategies/{sid}/{vid}'
        if state['runtime']:return RedirectResponse(base+'/dashboard',303)
        latest=next((r for r in reversed(state['evaluations']) if r['status']=='SUCCEEDED'),None)
        body=header(3,base)+'<h1>部署纸面账户</h1>'
        if not latest:
            return page('部署',body+f'<p class="warn">当前版本需要完成规则检验。</p><a class="btn primary" href="{base}">返回规则检验</a>')
        body+=f'<p class="policy-meta">{esc(spec["name"])} · 版本 {vid[:8]}</p>'
        body+=f'<dl><dt>初始现金</dt><dd>{spec["cash"]:,.2f} 元</dd><dt>初始持仓</dt><dd>{len(spec["holdings"])} 只</dd><dt>启用日</dt><dd>今日启用，最早下一交易日模拟执行</dd></dl>'
        body+=f'<p><a href="{base}/edit">修改账户初值（新版本）</a></p>'
        body+='<p>仅纸面模拟，不连接券商，不下单。历史检验不计入前向成绩。</p>'
        body+=f'<form method="post" action="{base}/start">'+hidden()+'<label class="row"><input type="checkbox" name="enabled" value="yes"> 每日自动运行（工作台开启时，17:30 后检查）</label><p class="mut">BaoStock 行情自动更新；CSV 行情及外部预测需要自行追加。电脑关闭时不运行。</p><button>开启纸面账户</button></form>'
        body+=f'<details><summary>高级：用历史演示运行流程</summary><p>仅使用存档当期预测，每条记录标记为历史；不会显示为前向成绩。</p><form method="post" action="{base}/start">'+hidden()+f'<input type="date" name="historical_start" value="{esc(latest["start"])}" required><button class="secondary">开启历史演示</button></form></details>'
        return page('部署纸面账户',body)

    @app.post("/strategies/{sid}/{vid}/{action}")
    async def action(req:Request,sid:str,vid:str,action:str):
        form=await req.form();guard(req,form.get("token"));base=f'/strategies/{sid}/{vid}'
        try:
            ws.revision(workdir,sid,vid)
            if action in {"evaluate","tick"}:
                config={"action":"strategy_"+action,"sid":sid,"vid":vid,
                        "ui":{"what":"strategy","name":"历史检验" if action=="evaluate" else "纸面记账","sid":sid,"vid":vid}}
                if action=="evaluate":
                    start,end=str(form["start"]),str(form["end"])
                    if pd.Timestamp(start)>pd.Timestamp(end):raise ValueError("起止日期顺序错误")
                    config.update(start=start,end=end)
                jid=submit("evaluate",config)
                return RedirectResponse(f'/job/{jid}',303)
            if action=="start":
                ws.start(workdir,sid,vid,historical_start=str(form.get("historical_start","")) or None)
                if form.get('enabled')=='yes':ws.automation(workdir,sid,vid,True)
                return RedirectResponse(base+'/dashboard',303)
            elif action=="automation":ws.automation(workdir,sid,vid,form.get("enabled")=="yes")
            else:raise HTTPException(404)
            return RedirectResponse(base,303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.get("/strategies/{sid}/{vid}/config.json")
    def download(sid:str,vid:str):
        return Response(json.dumps(get(sid,vid)["spec"],ensure_ascii=False,indent=2),media_type="application/json",
                        headers={"Content-Disposition":"attachment; filename=strategy-config.json"})

    @app.get('/strategies/{sid}/{vid}/scorecard.{format}')
    def report_export(sid:str,vid:str,format:str):
        if format not in {'json','md'}:raise HTTPException(404)
        root,_=ws.revision(workdir,sid,vid,verify_implementation=False)
        model=ex.read(root/'model-evaluation.json') if (root/'model-evaluation.json').is_file() else None
        report=scorecard_export.build(get(sid,vid),model)
        return Response(scorecard_export.render(report,format),
            media_type='application/json' if format=='json' else 'text/markdown; charset=utf-8',
            headers={'Content-Disposition':'attachment; filename=scorecard.'+format})

    @app.get('/strategies/{sid}/{vid}/rule-card.json')
    def frozen_rules(sid:str,vid:str):
        root,_=ws.revision(workdir,sid,vid,verify_implementation=False)
        meta=ex.read(root/'final_model/meta.json')
        if meta.get('model')!='rule_card':raise HTTPException(404)
        return Response(rule_cards.dumps(meta['rule_card']),media_type='application/json',
            headers={'Content-Disposition':'attachment; filename=rule-card.json'})

    @app.get('/strategies/{sid}/{vid}/policy-template.json')
    def share_template(sid:str,vid:str):
        return Response(json.dumps(policy_templates.export(get(sid,vid)['spec']),ensure_ascii=False,indent=2),
            media_type='application/json',headers={'Content-Disposition':'attachment; filename=policy-template.json'})

    @app.get('/policy-templates')
    def templates():
        body='<h1>复用一套政策规则</h1><p>导入 JSON 模板后，选择自己的信号、资金和持仓。模板不带收益保证，必须重新检验。</p><form method="post" action="/policy-templates/import" enctype="multipart/form-data">'+hidden()+'<label class="f">政策模板 JSON<input type="file" name="template" accept=".json" required></label><button>校验模板，选择我的信号</button></form>'
        return page('政策模板',body)

    @app.post('/policy-templates/import')
    async def import_template(req:Request):
        form=await req.form();guard(req,form.get('token'))
        try:
            sid=policy_templates.install(workdir,await form['template'].read(64001))
            return RedirectResponse('/strategies/new?template='+sid,303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.get("/strategies/{sid}/{vid}/records.csv")
    def csv(sid:str,vid:str):
        state=get(sid,vid)
        rows=[dict(day=r["day"],scope=r["scope"],recorded_at=r["recorded_at"],spec_hash=r["spec_hash"],
                   constraint_violations=json.dumps(r["constraint_violations"],ensure_ascii=False),**r["path"]) for r in state["records"]]
        return Response(pd.DataFrame(rows,columns=["day","scope","recorded_at","spec_hash","date","equity","cash","exposure","total_cost","constraint_violations"]).to_csv(index=False),media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":"attachment; filename=strategy-records.csv"})

    @app.get("/strategies/{sid}/{vid}/decisions")
    def decisions(sid:str,vid:str):
        state=get(sid,vid);root,_=ws.revision(workdir,sid,vid,verify_implementation=False)
        records=state["records"];body=header(4 if state["runtime"] else 3,f'/strategies/{sid}/{vid}')+'<h1>每一步为什么这样做</h1>'
        if records:
            rows=[r["decision"] for r in records if r.get("decision")]
            fills=[f for r in records for f in r["fills"]]
            body+='<p>以下是纸面运行记录；实际成交指模拟成交。</p>'
        else:
            paths=[p.parent for p in sorted(root.glob("evaluations/*/result.json"),key=lambda p:p.stat().st_mtime) if ex.read(p)["status"]=="SUCCEEDED"]
            result=ex.read(paths[-1]/"selected.json") if paths else {"decisions":[],"fills":[]}
            rows,fills=result["decisions"],result["fills"]
            body+='<p>以下是历史检验，不是前向记录。</p>'
        for row in rows:
            body+=f'<details><summary>{esc(row["date"])} · 目标仓位 {row["target_exposure"]:.1%} · 执行日 {esc(row["execution_date"])}</summary>'
            body+=f'<p>风险方式：{esc(REASONS.get(row["risk_reason"],row["risk_reason"]))} · 最近 ML 拟合 {esc(row["risk_fit_day"] or "未使用")}</p>'
            body+='<p>先看原有持仓漂移，再看立即调整目标，最后看分步和硬限制后的目标。目标没有成交承诺。</p>'
            body+='<div class="tw"><table><tr><th>股票</th><th>原持仓权重</th><th>立即调整目标</th><th>最终目标</th></tr>'
            codes=sorted(set(row["drift_weights"])|set(row["weights"]))
            for code in codes:
                body+=f'<tr><td>{esc(code)}</td>'+''.join(f'<td>{row[k].get(code,0):.2%}</td>' for k in ("drift_weights","immediate_weights","weights"))+'</tr>'
            body+='</table></div><p>'+ '；'.join(esc(REASONS.get(r["reason"],r["reason"])) for r in row["reasons"])+'</p>'
            done=[f for f in fills if f["date"]==row["execution_date"]]
            body+='<h3>模拟成交 / 受阻</h3>'
            if not done:body+='<p>尚无该次执行记录，或没有产生买卖。</p>'
            for f in done:
                body+=f'<p>{esc(f["asset_id"])} · {esc(f["side"])} · {f["quantity"]} 股 · 费用 {f["total_cost"]:.2f} 元 · {esc(reason_text(f["reason"]))}</p>'
                if f.get('cost_model')=='struct_v1':body+=f'<p class="mut">其中报价半价差估计 {f["struct_spread"]:.2f} 元（{f["estimated_half_spread_bp"]:.3f} bp，上月输入 {esc(f["cost_inputs_month"])}）；佣金 {f["commission"]:.2f}，印花税 {f["stamp_tax"]:.2f}，额外压力 {f["slippage"]:.2f}。不含实际冲击标定。</p>'
            body+='</details>'
        if records:
            for record in records:
                if record["constraint_violations"]:
                    body+=f'<p class="warn">{esc(record["day"])} · 实际约束偏离：{esc(json.dumps(record["constraint_violations"],ensure_ascii=False))}</p>'
        return page("决策回放",body)

    @app.get("/policy-schema.json")
    def schema():
        return policy.schema()
