"""Progressive policy editor and daily dashboard for frozen strategy versions."""
from __future__ import annotations

from dataclasses import asdict
import html
import json

import pandas as pd
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from . import experiments as ex, policy, strategy, strategy_workspace as ws
from .governance_ui import REASONS
from .strategy_ui import reason_text


def esc(value):
    return html.escape(str(value), quote=True)


def register(app, workdir, *, page, guard, token, locate, submit, datasets):
    def hidden():
        return f'<input type="hidden" name="token" value="{token}">'

    def error(exc):
        response=page("需要调整",f'<h1>这一步没有完成</h1><p class="box bad">{esc(exc)}</p>'
                      '<p><a href="/strategies">返回我的策略</a> · <a href="javascript:history.back()">返回修改</a></p>')
        response.status_code=400
        return response

    def header(step, base=""):
        names=["选择信号","配置政策","历史检验","纸面运行"]
        text='<style>[data-field]{display:grid!important;grid-template-columns:minmax(160px,1fr) minmax(100px,160px);gap:6px 12px;width:min(100%,640px);max-width:100%!important;align-items:center;margin-block:14px!important}[data-field] input{width:100%}[data-field] small{grid-column:1/-1;color:var(--mut);display:block}[data-field][hidden]{display:none!important}.policy-steps{display:flex;flex-wrap:wrap;gap:10px;margin:22px 0}.policy-steps span{padding:8px 12px;border-radius:8px;background:var(--card)}.policy-steps .current{background:var(--accbg);color:var(--acc)}.policy-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:14px}.policy-cards .box{margin:0}code{overflow-wrap:anywhere}.policy-chart{width:100%;height:auto}</style>'
        text+='<div class="policy-steps" aria-label="策略步骤">'+''.join(
            f'<span class="{"current" if step==i else ""}" {"aria-current=step" if step==i else ""}>{i} · {name}</span>'
            for i,name in enumerate(names,1))+'</div>'
        return '<p><a href="/strategies">← 我的策略</a></p>'+text

    def get(sid,vid):
        try:return ws.inspect(workdir,sid,vid)
        except (ValueError,OSError,KeyError) as exc:raise HTTPException(409,str(exc)) from exc

    @app.get("/strategies")
    def index():
        body='<h1>我的策略</h1><p>在本机训练模型，再按自己的持仓和纪律配置治理、检验与运行。先用默认设置，需要时展开高级选项。</p>'
        body+='<p><a class="btn" href="/">训练一个模型</a> <a class="btn secondary" href="/strategies/new">使用已有模型，新建策略</a></p>'
        for identity in sorted((workdir/"runs/strategies").glob("*/identity.json"),key=lambda p:p.stat().st_mtime,reverse=True):
            family=ex.read(identity)
            body+=f'<section class="box"><h2>{esc(family["name"])}</h2>'
            for path in reversed(ws.versions(workdir,identity.parent.name)):
                spec=ex.read(path)
                runtime=ex.read(path.parent/"runtime.json") if (path.parent/"runtime.json").exists() else None
                health=ex.read(path.parent/"health.json") if (path.parent/"health.json").exists() else {}
                body+=f'<p><a href="/strategies/{spec["sid"]}/{spec["vid"]}">版本 {spec["vid"][:8]}</a> · {esc(spec["name"])} · '
                body+=esc(health.get("message","待检验 / 待启用"))
                if runtime:body+=' · '+("历史演示" if runtime["scope"]=="historical_replay" else "纸面观察")
                body+='</p>'
            body+='</section>'
        return page("我的策略",body)

    @app.get("/strategies/new")
    def new(source:str=""):
        sources=sorted({p.parent.name for p in (workdir/"runs").glob("*/predictions.parquet")}
                       |{p.parent.name for p in (workdir/"runs/history").glob("*/predictions.parquet")})
        options=''
        for candidate in sources:
            try:
                path=locate(candidate)
                if not (path/"final_model/meta.json").is_file():continue
                meta=ex.read(path/"final_model/meta.json")
                strategy.locked_recipe(meta)
                options+=f'<option value="{esc(candidate)}" {"selected" if candidate==source else ""}>{esc(candidate)} · 训练截止 {esc(meta["data_end"])}</option>'
            except (ValueError,OSError,KeyError,HTTPException):continue
        data_options=''.join(f'<option value="{esc(k)}">{esc(k)}</option>' for k in datasets(workdir) if k!="_trial")
        body=header(1)+'<h1>先选给股票排名的模型</h1><p>信号负责打分，下一步才决定怎么买。这里使用已有训练模型的存档预测检验历史，用冻结模型处理启用后的新行情。</p>'
        body+='<p class="mut">官方预训练模型和外部预测专用接入向导尚未发布。现在可以选择自己的已有模型，或<a href="/">先训练</a>。</p>'
        body+='<form action="/strategies/configure" method="get">'
        body+=f'<label class="f">信号来源<select name="source" required>{options}</select></label>'
        body+=f'<label class="f">持续更新的数据源<select name="dataset" required>{data_options}</select></label>'
        body+='<button>下一步：配置政策</button></form>'
        return page("选择信号",body)

    def editor(source,dataset,previous=None):
        old=previous["spec"] if previous else None
        resolved=old["resolved"] if old else policy.resolve("direct")
        chosen=resolved["policy"]; values=chosen["parameters"]
        body=header(2)+'<h1>你来定怎么使用排名</h1><p>先选预设、填可用现金；高级设置只在需要时展开。每五个交易日决策，下一开市日开盘模拟执行。</p>'
        body+='<form method="post" action="/strategies" enctype="multipart/form-data">'+hidden()
        body+=f'<input type="hidden" name="source" value="{esc(source)}"><input type="hidden" name="dataset" value="{esc(dataset)}">'
        if old:body+=f'<input type="hidden" name="sid" value="{old["sid"]}"><input type="hidden" name="parent" value="{old["vid"]}">'
        body+=f'<label class="f">策略名称<input name="name" maxlength="80" value="{esc(old["name"] if old else "我的策略")}" required></label>'
        body+=f'<label class="f">可用现金（元）<input name="cash" type="number" min="0" step="0.01" value="{old["cash"] if old else 100000}" required></label>'
        body+='<label class="f">政策预设<select name="preset" id="policy-preset">'+''.join(
            f'<option value="{k}" {"selected" if k==resolved["preset"] else ""}>{esc(v["name"])}</option>' for k,v in policy.PRESETS.items())+'</select></label>'
        body+='<p id="preset-help" class="mut"></p><button type="button" class="secondary" id="reset-policy">恢复该预设</button><p class="mut">切换预设会载入该预设的参数。分步调整来自自有研究的机制，默认 0.5 是待验证的工程选择，不承诺更赚钱。</p>'
        body+='<details id="advanced"><summary>高级设置 · 微调纪律和实际费用</summary>'
        body+='<label class="f">风险控制<select name="mode" id="policy-mode">'+''.join(
            f'<option value="{k}" {"selected" if k==chosen["mode"] else ""}>{v}</option>' for k,v in
            (("fixed","固定仓位"),("ewma","EWMA 波动目标"),("ml","ML 风险辅助 · 实验")))+'</select></label>'
        ui_schema=policy.schema()
        definition=next(v for v in ui_schema["$defs"].values() if "n_hold" in v.get("properties",{}))
        for key,field in definition["properties"].items():
            info=ui_schema["ui"][key]
            bounds=''.join(f' {attr}="{field[k]}"' for k,attr in (("minimum","min"),("exclusiveMinimum","min"),("maximum","max")) if k in field)
            body+=f'<label class="f" data-field="{key}">{esc(info["label"])}（{esc(info["unit"])}）<input name="{key}" type="number" step="{"1" if field["type"]=="integer" else "any"}" value="{values[key]}"{bounds} required><small>{esc(info["help"])}</small></label>'
        body+='<p class="mut">比例用小数：0.1 = 10%。单票上限超出部分留现金。目标约束不能保证跳空或受阻后的实际持仓仍达标。</p>'
        body+='<label class="f">现有持仓 CSV（可选）<input type="file" name="holdings" accept=".csv"></label><p><a href="/account-template.csv">下载持仓模板</a>；列为 code,quantity,sellable_quantity。</p>'
        if old and old["holdings"]:body+='<p>未上传新文件时保留旧版本的初始持仓。</p>'
        body+='</details><details><summary>固定执行口径与费用说明</summary><p>T+1、申报数量、涨跌停、按日期生效的印花税及收盘决策口径固定。佣金和最低佣金可以按券商填写；价差与冲击未标定。遇到持仓公司行动或行情缺口停止。</p></details>'
        body+='<button>冻结设置，进入检验</button></form>'
        presets={k:policy.resolve(k)["policy"]|{"description":v["description"]} for k,v in policy.PRESETS.items()}
        data=json.dumps(presets,ensure_ascii=False).replace("<","\\u003c")
        body+='<script>(()=>{const presets='+data+';const p=document.querySelector("#policy-preset"),m=document.querySelector("#policy-mode");function show(){document.querySelector("[data-field=target_volatility]").hidden=m.value==="fixed";document.querySelector("#preset-help").textContent=presets[p.value].description;}function reset(){const v=presets[p.value];m.value=v.mode;Object.entries(v.parameters).forEach(([k,x])=>document.querySelector("input[name="+k+"]").value=x);show();}p.addEventListener("change",reset);document.querySelector("#reset-policy").addEventListener("click",reset);m.addEventListener("change",show);show();})();</script>'
        return page("配置政策",body)

    @app.get("/strategies/configure")
    def configure(source:str,dataset:str):
        try:
            locate(source);datasets(workdir)[dataset]
            return editor(source,dataset)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.get("/strategies/{sid}/{vid}/edit")
    def edit(sid:str,vid:str):
        state=get(sid,vid)
        return editor(state["spec"]["signal"]["source"],state["spec"]["data_feed"],state)

    @app.post("/strategies")
    async def create(req:Request):
        form=await req.form();guard(req,form.get("token"))
        try:
            parent=str(form.get("parent",""));sid=str(form.get("sid","")) or None
            old=ws.revision(workdir,sid,parent,verify_implementation=False) if parent and sid else None
            source=old[0] if old else locate(str(form["source"]))
            feed=old[1]["data_feed"] if old else datasets(workdir)[str(form["dataset"])]["path"]
            preset=str(form["preset"]);base=policy.resolve(preset)["policy"]["parameters"]
            overrides={}
            for k,v in base.items():
                if k not in form:continue
                x=int(form[k]) if type(v) is int else float(form[k])
                if x!=v:overrides[k]=x
            uploaded=form.get("holdings");holdings=old[1]["holdings"] if old else []
            if uploaded and getattr(uploaded,"filename",""):
                holdings=strategy.read_holdings(await uploaded.read(2_000_001))
            sid,vid=ws.create(workdir,source,feed,name=str(form["name"]),cash=float(form["cash"]),
                preset=preset,overrides=overrides,mode=str(form["mode"]),holdings=holdings,sid=sid)
            return RedirectResponse(f"/strategies/{sid}/{vid}",303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.get("/strategies/{sid}/{vid}")
    def dashboard(sid:str,vid:str):
        state=get(sid,vid);spec=state["spec"];base=f'/strategies/{sid}/{vid}'
        results=state["evaluations"];success=[r for r in results if r["status"]=="SUCCEEDED"]
        latest=success[-1] if success else None
        body=header(4 if state["runtime"] else 3)+f'<h1>{esc(spec["name"])}</h1><p>版本 {vid[:8]} · {esc(policy.PRESETS[spec["resolved"]["preset"]]["name"])} · {esc(spec["resolved"]["policy"]["mode"])}'
        body+=f' · <a href="{base}/edit">修改并建立新版本</a></p>'
        body+='<div class="policy-cards"><section class="box"><h2>可运行性</h2>'
        operability=latest.get("operability") if latest else None
        if operability:body+=f'<p>{esc(operability["message"])}</p>'
        else:body+='<p>启用前检查信号、交易日历和最小买入数量；通过不代表无泄漏或一定成交。</p>'
        body+='</section><section class="box"><h2>经济增量</h2><p>证据不足</p><p>历史差异只描述这段路径，不能证明未来更好。</p></section>'
        body+=f'<section class="box"><h2>运行健康</h2><p>{esc(state["health"]["message"])}</p></section></div>'
        body+=f'<p>本版本已记录 {len(results)} 次检验；本策略有 {len(ws.versions(workdir,sid))} 个版本。记录只覆盖本地可见尝试。</p>'
        predictions=pd.read_parquet(ws.location(workdir,sid)/"versions"/vid/"predictions.parquet",columns=["decision_time"])
        dates=pd.to_datetime(predictions.decision_time,utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
        if not state["runtime"]:
            body+='<h2>如果过去这样做，会怎样？</h2><p>与相同信号、风险方式和授权的“立即调整”比较。只改变调整速度；双方敞口可能不同，收益差不直接归因于算法。</p>'
            body+=f'<form method="post" action="{base}/evaluate">'+hidden()
            body+=f'<label class="f">开始<input type="date" name="start" value="{esc(dates.min())}" required></label>'
            body+=f'<label class="f">结束<input type="date" name="end" value="{esc(dates.max())}" required></label><button>记录尝试并检验</button></form>'
        if latest:
            body+='<h2>历史对照</h2><p>蓝色：当前政策 · 灰色：立即调整</p>'
            root,_=ws.revision(workdir,sid,vid,verify_implementation=False)
            dirs=[p.parent for p in sorted(root.glob("evaluations/*/result.json"),key=lambda p:p.stat().st_mtime) if ex.read(p)["status"]=="SUCCEEDED"]
            arms=[ex.read(dirs[-1]/(k+".json")) for k in ("selected","immediate")]
            values=[[row["equity"]/arm["path"][0]["equity"] for row in arm["path"]] for arm in arms]
            lo,hi=min(min(v) for v in values),max(max(v) for v in values);span=max(hi-lo,.001)
            lines=''
            for v,color in zip(values,("#2984dd","#8c929c")):
                points=' '.join(f'{30+i*880/max(1,len(v)-1):.2f},{210-(x-lo)*180/span:.2f}' for i,x in enumerate(v))
                lines+=f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>'
            axes=''.join(f'<line x1="30" x2="910" y1="{y}" y2="{y}" stroke="#8c929c" opacity=".2"/>'
                f'<text x="32" y="{y-5}" fill="#737d8b" font-size="12">{value:.3f}</text>'
                for value,y in ((lo,210),((hi+lo)/2,120),(hi,30)))
            axes+=f'<text x="30" y="235" fill="#737d8b" font-size="12">{esc(latest["start"])}</text><text x="910" y="235" text-anchor="end" fill="#737d8b" font-size="12">{esc(latest["end"])}</text>'
            body+='<svg class="policy-chart" viewBox="0 0 940 250" role="img" aria-label="历史归一净值对照">'+axes+lines+'</svg>'
            body+='<div class="tw"><table><tr><th>政策</th><th>收益</th><th>最大回撤</th><th>平均实际仓位</th><th>已计费用（元）</th><th>交易额 / 权益累计</th></tr>'
            for key,label in (("selected","当前政策"),("immediate","立即调整")):
                m=latest["metrics"][key]
                body+=f'<tr><td>{label}</td><td>{m["total_return"]:.2%}</td><td>{m["max_drawdown"]:.2%}</td><td>{m["mean_exposure"]:.2%}</td><td>{m["total_cost"]:.2f}</td><td>{m["gross_traded_fraction"]:.2f}</td></tr>'
            body+='</table></div>'+f'<p>{latest["metrics"]["selected"]["sessions"]} 段日收益；{esc(latest["message"])}</p>'
            difference=latest["cost_difference"]
            body+=f'<p>这段路径已计费用比立即调整{"多" if difference>0 else "少"} {abs(difference):.2f} 元；请同时比较平均实际仓位。费用差包含交易规模和持仓路径变化，不代表全部成本或收益增量。</p>'
        failures=[r for r in results if r["status"]!="SUCCEEDED"]
        if failures:body+='<details><summary>未完成的尝试</summary>'+''.join(f'<p>{esc(r["status"])} · {esc(r.get("error","运行中"))}</p>' for r in failures)+'</details>'
        if latest and not state["runtime"]:
            body+='<h2>从今天开始观察</h2><p>按本版本设置开启独立纸面账户。历史成绩不会转成前向成绩。默认不开定时检查。</p>'
            body+=f'<form method="post" action="{base}/start">'+hidden()+'<button>开启纸面观察</button></form>'
            body+=f'<details><summary>高级：用历史演示运行流程</summary><p>仅使用存档当期预测，每条记录标记为历史；不会显示为前向成绩。</p><form method="post" action="{base}/start">'+hidden()+f'<input type="date" name="historical_start" value="{esc(latest["start"])}" required><button class="secondary">开启历史演示</button></form></details>'
        if state["runtime"]:
            runtime=state["runtime"];records=state["records"]
            counts={scope:sum(r["scope"]==scope for r in records) for scope in ("forward_paper","late_reconstruction","historical_replay")}
            body+='<h2>日常运行</h2>'+f'<p>启用日 {esc(runtime["start_date"])} · 按时纸面记录 {counts["forward_paper"]} 天 · 迟到重建 {counts["late_reconstruction"]} 天 · 历史演示 {counts["historical_replay"]} 天</p>'
            body+='<p>自动检查只在工作台开启时运行，17:30 后每天尝试一次，读取已经更新的数据源；当前不自动下载行情。手动检查可在补数据后重试。建议清单是纸面目标，不是已经成交的实盘订单。</p>'
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
        body+='<details><summary>实际生效值、修改与来源</summary><p>以下冻结配置同时用于检验和运行。固定模式的 target_volatility 不生效。</p>'
        body+=f'<pre class="plain">{esc(json.dumps(spec["resolved"],ensure_ascii=False,indent=2))}</pre>'
        body+=f'<p>配置哈希 <code>{esc(spec["spec_hash"])}</code></p></details>'
        body+='<p class="mut">未独立认证上游无前视；已计佣金、按日印花税与自填滑点压力，未标定价差与冲击。公司行动尚不支持。</p>'
        return page(spec["name"],body)

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
            if action=="start":ws.start(workdir,sid,vid,historical_start=str(form.get("historical_start","")) or None)
            elif action=="automation":ws.automation(workdir,sid,vid,form.get("enabled")=="yes")
            else:raise HTTPException(404)
            return RedirectResponse(base,303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.get("/strategies/{sid}/{vid}/config.json")
    def download(sid:str,vid:str):
        return Response(json.dumps(get(sid,vid)["spec"],ensure_ascii=False,indent=2),media_type="application/json",
                        headers={"Content-Disposition":"attachment; filename=strategy-config.json"})

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
        records=state["records"];body=header(4 if records else 3)+'<h1>每一步为什么这样做</h1>'
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
            for f in done:body+=f'<p>{esc(f["asset_id"])} · {esc(f["side"])} · {f["quantity"]} 股 · 费用 {f["total_cost"]:.2f} 元 · {esc(reason_text(f["reason"]))}</p>'
            body+='</details>'
        if records:
            for record in records:
                if record["constraint_violations"]:
                    body+=f'<p class="warn">{esc(record["day"])} · 实际约束偏离：{esc(json.dumps(record["constraint_violations"],ensure_ascii=False))}</p>'
        return page("决策回放",body)

    @app.get("/policy-schema.json")
    def schema():
        return policy.schema()
