"""CSV-only onboarding for external forecasts, without executing model files."""
from __future__ import annotations

import html
import json
import re

from fastapi import Request
from fastapi.responses import RedirectResponse,Response

from . import experiments as ex,external_signals as signals,strategy_projects as projects


def register(app,workdir,*,page,guard,token):
    def esc(value):return html.escape(str(value),quote=True)
    def hidden():return f'<input type="hidden" name="token" value="{token}">'
    def error(exc):
        response=page('外部预测未导入',f'<h1>需要修正预测文件或口径</h1><p>{esc(exc)}</p><a href="/signals/import">返回导入向导</a>')
        response.status_code=400;return response
    def feed(sid):
        if not ex.ID_RE.fullmatch(sid):raise ValueError('外部信号编号无效')
        return workdir/'signals'/sid

    @app.get('/signals/import')
    def wizard(project:str=''):
        if project:
            try:projects.read(workdir,project)
            except (ValueError,OSError,KeyError) as exc:return error(exc)
        return page('导入外部预测','<p><a href="/strategies/new">← 选择模型</a></p><h1>导入外部预测</h1>'
            '<form action="/signals/import" method="post" enctype="multipart/form-data">'+hidden()+f'<input type="hidden" name="project" value="{project}">'+
            '<label class="f">信号名称<input name="name" maxlength="80" required></label>'
            '<label class="f">模型版本<input name="version" maxlength="80" placeholder="model-v1" required></label>'
            '<label class="f">完整股票池<textarea name="codes" rows="4" placeholder="600000.SH,000001.SZ" required></textarea></label>'
            '<label class="f">决策时刻<select name="decision_time"><option>15:30</option></select></label>'
            '<label class="f">预测期限<select name="horizon"><option value="5">五交易日：次日开盘到第 6 日开盘</option></select></label>'
            '<label class="f">预测 CSV<input type="file" name="predictions" accept=".csv" required></label>'
            '<p><a href="/signals/template.csv">下载 CSV 模板</a></p>'
            '<button>校验并保存信号来源</button></form>')

    @app.get('/signals/template.csv')
    def template():
        return Response('code,decision_time,prediction,generated_at\n600000.SH,2026-10-22T15:30:00+08:00,0.8,2026-10-22T15:20:00+08:00\n000001.SZ,2026-10-22T15:30:00+08:00,0.2,2026-10-22T15:20:00+08:00\n',media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=external-predictions.csv'})

    @app.post('/signals/import')
    async def imported(req:Request):
        form=await req.form();guard(req,form.get('token'))
        try:
            project=str(form.get('project',''))
            if project:projects.read(workdir,project)
            c=signals.contract(str(form['name']),str(form['version']),[x for x in re.split(r'[\s,，]+',str(form['codes']).strip()) if x],horizon=int(form['horizon']),decision_time=str(form['decision_time']))
            upload=form['predictions'];raw=await upload.read(32_000_001)
            source=signals.create(workdir,c,raw)
            if project:return RedirectResponse('/strategies/new?project='+project+'&source='+source.name,303)
            return RedirectResponse('/signals/'+source.name.removeprefix('external_'),303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.get('/signals/{sid}')
    def show(sid:str):
        try:
            c,frame,receipts=signals.current(feed(sid))
            body=f'<h1>{esc(c["name"])}</h1><p>外部版本 {esc(c["version"])} · {len(c["codes"])} 只股票 · 五日预测 · 15:30 决策（上海时区）</p><p>已收到 {frame.decision_time.str[:10].nunique()} 个决策日，覆盖到 {esc(frame.decision_time.max())}。声明的生成时间与平台首次接收时间分别保留，未认证上游无前视。</p>'
            body+=f'<a class="btn" href="/strategies/new?source=external_{sid}">下一步：选择行情并配置政策</a>'
            body+=f'<h2>追加预测</h2><form action="/signals/{sid}/append" method="post" enctype="multipart/form-data">'+hidden()+'<label class="f">新预测 CSV<input type="file" name="predictions" accept=".csv" required></label><button>校验并追加预测</button></form>'
            body+='<details><summary>契约与首次接收记录</summary><pre class="plain">'+esc(json.dumps({'contract':c,'first_receipts':receipts},ensure_ascii=False,indent=2))+'</pre></details>'
            return page(c['name'],body)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.post('/signals/{sid}/append')
    async def append(req:Request,sid:str):
        form=await req.form();guard(req,form.get('token'))
        try:
            signals.append(feed(sid),await form['predictions'].read(32_000_001))
            return RedirectResponse('/signals/'+sid,303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)
