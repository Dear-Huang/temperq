"""Staged CSV import: preview, explicit field mapping, validation and save."""
from __future__ import annotations

import csv
import html
import io
import uuid

import pandas as pd
from fastapi import Request
from fastapi.responses import RedirectResponse

from . import csvimport as ci, experiments as ex, strategy_projects as projects, daily_data

LABELS=dict(zip(ci.FIELDS,['代码','日期','开盘价','最高价','最低价','收盘价','成交额','复权因子','是否 ST','涨停价','跌停价','成交量']))
LIMIT=128_000_000


def register(app,workdir,*,page,guard,token,data_root,datasets):
    def esc(value):return html.escape(str(value),quote=True)
    def hidden():return f'<input type="hidden" name="token" value="{token}">'
    def root(uid):
        if not ex.ID_RE.fullmatch(uid):raise ValueError('上传编号无效')
        path=workdir/'runs/csv_uploads'/uid
        if not (path/'upload.csv').is_file():raise ValueError('上传记录不存在，请重新选择文件')
        return path
    def actions(project=''):
        return '<p><a href="/template.csv">下载 CSV 模板</a> · <a href="/train'+('?project='+project if project else '')+'#free-data">改用免费数据（BaoStock）</a></p>'
    def error(exc,project=''):
        response=page('导入未完成','<h1>需要调整数据</h1><p class="warn">'+esc(exc)+'</p>'+actions(project))
        response.status_code=400
        return response
    def feed_for(name):
        current=daily_data.current(datasets(workdir)[name]['path'])
        if current.parent.name!='snapshots' or not (current.parent.parent/'current.json').is_file():
            raise ValueError('此旧数据源没有增量快照，请使用新的 CSV 数据源或高级数据更新入口')
        return current.parent.parent
    def validate(path,state):
        calendar=(path/'calendar.csv').read_bytes() if (path/'calendar.csv').is_file() else None
        if state.get('append'):
            if calendar is None:raise ValueError('追加行情需要完整交易日历；已保留列映射，请上传更新后的日历')
            canonical=ci.read(path/'upload.csv',mapping=state['mapping'],amount_unit=state['amount_unit'])
            return daily_data.update(feed_for(state['append']),canonical.to_csv(index=False).encode(),calendar,dry_run=True)
        return ci.import_csv(path/'upload.csv',data_root,state['name'],mapping=state['mapping'],amount_unit=state['amount_unit'],dry_run=True,calendar_raw=calendar)
    def selection_hash(path,state):
        return ex.object_digest({'name':state['name'],'mapping':state['mapping'],'unit':state['amount_unit'],
            'calendar':ex.digest(path/'calendar.csv') if (path/'calendar.csv').is_file() else None})
    def preview(uid,message='',checked=False):
        path=root(uid);state=ex.read(path/'state.json')
        frame=pd.read_csv(path/'upload.csv',dtype=str,nrows=5,keep_default_na=False)
        body='<h1>导入行情 CSV</h1><p class="policy-meta">上传 → 对应列 → 检查 → 保存</p>'
        if message:body+='<p class="warn" role="alert">'+esc(message)+'</p>'
        body+=f'<form action="/data/csv/{uid}/check" method="post" enctype="multipart/form-data">'+hidden()
        body+=f'<label class="f">数据名称<input name="name" value="{esc(state["name"])}"'+(' readonly' if state.get('append') else '')+' required pattern="[A-Za-z0-9_-]{1,40}"></label>'
        body+='<div class="tw"><table><thead><tr>'
        for i,column in enumerate(frame.columns):
            chosen=state['mapping'].get(column,'')
            options='<option value="">忽略</option>'+''.join(f'<option value="{key}"'+(' selected' if key==chosen else '')+f'>{label}</option>' for key,label in LABELS.items())
            body+=f'<th>{esc(column)}<select name="column_{i}" aria-label="{esc(column)} 对应字段">{options}</select></th>'
        body+='</tr></thead><tbody>'
        for row in frame.itertuples(index=False,name=None):body+='<tr>'+''.join('<td>'+esc(value)+'</td>' for value in row)+'</tr>'
        body+='</tbody></table></div><label class="f">成交额单位<select name="amount_unit">'
        for key,label in [('yuan','元'),('thousand','千元'),('ten_thousand','万元')]:
            body+=f'<option value="{key}"'+(' selected' if key==state['amount_unit'] else '')+f'>{label}</option>'
        body+='</select></label><details><summary>交易日历（规则检验需要）</summary><p class="mut">上传数据供应方的交易日历 CSV：date、is_open。开市日为 1，休市日为 0；覆盖行情期间的每一天。</p><input type="file" name="calendar" accept=".csv">'+('<p>已保留上传的日历。</p>' if (path/'calendar.csv').is_file() else '')+'</details><button'+(' class="secondary"' if checked else '')+'>检查数据</button></form>'+actions(state.get('project',''))
        if checked:
            snap=validate(path,state)
            body+=f'<section class="policy-next" id="csv-confirmation"><h2>检查完成</h2><p>{snap["start"]} 至 {snap["end"]} · {snap["codes"]} 只 · {snap["rows"]:,} 行</p>'
            body+=''.join('<p class="mut">'+esc(note)+'</p>' for note in snap['notes'])
            if not (path/'calendar.csv').is_file():body+='<p class="warn">尚无交易日历。可以先训练；进入账户规则前需要补齐，或改用带日历的免费行情。</p>'
            body+=f'<form action="/data/csv/{uid}/save" method="post">'+hidden()+f'<input type="hidden" name="checked_hash" value="{state["checked_hash"]}"><button>保存并继续</button></form></section>'
            body+='''<script>(()=>{const result=document.getElementById('csv-confirmation');
              const form=document.querySelector('form[action$="/check"]');
              form.addEventListener('input',()=>{result.hidden=true;form.querySelector('button').classList.remove('secondary')});
              form.addEventListener('change',()=>{result.hidden=true;form.querySelector('button').classList.remove('secondary')});})();</script>'''
        return page('对应 CSV 列',body)

    @app.get('/data')
    def library(project:str='',append:str=''):
        if project:
            try:projects.read(workdir,project)
            except (ValueError,OSError,KeyError) as exc:return error(exc)
        body='<div class="page-heading"><h1>数据</h1><a href="/train'+('?project='+project if project else '')+'#free-data">下载免费日线</a></div>'
        if append:
            try:
                feed=feed_for(append)
                if not (feed/'csv_mapping.json').is_file():raise ValueError('此来源没有保存的 CSV 映射，请通过原数据适配器更新')
            except (ValueError,OSError,KeyError) as exc:return error(exc)
        body+='<form action="/data/csv" method="post" enctype="multipart/form-data">'+hidden()+f'<input type="hidden" name="project" value="{project}"><input type="hidden" name="append" value="{esc(append)}">'
        body+='<label class="f">数据名称<input name="name" required pattern="[A-Za-z0-9_-]{1,40}" placeholder="my_data" value="'+esc(append)+'"'+(' readonly' if append else '')+'></label><label class="f">行情 CSV<input type="file" name="file" accept=".csv" required></label><button>上传并对应列</button></form>'+actions(project)
        body+='<h2>已有数据</h2><div class="tw"><table><tr><th>名称</th><th>覆盖日期</th><th>股票数</th><th></th></tr>'
        for name,item in datasets(workdir).items():
            if name=='_trial':continue
            snap=item['snap']
            body+=f'<tr><td>{esc(name)}</td><td>{esc(snap["start"])} — {esc(snap["end"])}</td><td>{esc(snap.get("codes","—"))}</td><td><a href="/data/{esc(name)}">管理</a></td></tr>'
            source=daily_data.current(item['path'])
            if source.parent.name=='snapshots' and (source.parent.parent/'csv_mapping.json').is_file():
                body+=f'<tr><td colspan="4"><a href="/data?append={esc(name)}">追加 {esc(name)} 行情</a></td></tr>'
            if not (source/'calendar.csv').is_file() and (source/'bars').is_dir():
                body+=f'<tr><td colspan="4"><details><summary>{esc(name)}：补充交易日历</summary><form method="post" action="/data/{esc(name)}/calendar" enctype="multipart/form-data">'+hidden()+'<label class="f">日历 CSV（date、is_open）<input type="file" name="calendar" accept=".csv" required></label><button>检查并保存日历</button></form></details></td></tr>'
        return page('数据',body+'</table></div>')

    @app.post('/data/{dsid}/calendar')
    async def add_calendar(req:Request,dsid:str):
        form=await req.form();guard(req,form.get('token'))
        try:
            source=daily_data.current(datasets(workdir)[dsid]['path'])
            if (source/'calendar.csv').exists():raise ValueError('已有交易日历不能覆盖，请新建数据来源')
            raw=await form['calendar'].read(2_000_001)
            if len(raw)>2_000_000:raise ValueError('交易日历超过 2 MB')
            dates=set()
            for path in (source/'bars').glob('*.parquet'):dates.update(pd.read_parquet(path,columns=['date']).date.astype(str))
            if not dates:raise ValueError('此来源没有可匹配的行情日期')
            calendar=ci.calendar_frame(raw,dates)
            calendar.to_csv(source/'calendar.csv',index=False)
            return RedirectResponse('/data',303)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.post('/data/csv')
    async def upload(req:Request):
        form=await req.form();guard(req,form.get('token'))
        try:
            project=str(form.get('project',''))
            if project:projects.read(workdir,project)
            up=form.get('file')
            if not up or not getattr(up,'filename',''):raise ValueError('请选择 CSV 文件')
            raw=await up.read(LIMIT+1)
            if len(raw)>LIMIT:raise ValueError('CSV 超过 128 MB，请分开导入或使用本地数据文件夹')
            headings=next(csv.reader(io.StringIO(raw.decode('utf-8-sig'))),[])
            if not headings or len(headings)!=len(set(headings)):raise ValueError('表头为空或重复，请使用唯一列名')
            frame=pd.read_csv(io.BytesIO(raw),dtype=str,nrows=5)
            if frame.empty:raise ValueError('文件是空的')
            uid=uuid.uuid4().hex;path=workdir/'runs/csv_uploads'/uid;path.mkdir(parents=True)
            (path/'upload.csv').write_bytes(raw)
            mapping={c:ci.ALIASES.get(c.strip().lower(),c.strip().lower()) for c in frame.columns}
            mapping={c:v if v in ci.FIELDS else '' for c,v in mapping.items()}
            append=str(form.get('append',''))
            unit='yuan'
            if append:
                profile=ex.read(feed_for(append)/'csv_mapping.json')
                mapping={c:profile['columns'].get(c,mapping.get(c,'')) for c in frame.columns}
                unit=profile['amount_unit']
            ex.write(path/'state.json',dict(name=str(form.get('name','my_data')),filename=up.filename,
                project=project,mapping=mapping,amount_unit=unit,append=append))
            return RedirectResponse('/data/csv/'+uid,303)
        except (ValueError,OSError,KeyError,UnicodeError) as exc:return error(exc)

    @app.get('/data/csv/{uid}')
    def show(uid:str):
        try:return preview(uid)
        except (ValueError,OSError,KeyError) as exc:return error(exc)

    @app.post('/data/csv/{uid}/check')
    async def check(req:Request,uid:str):
        form=await req.form();guard(req,form.get('token'))
        try:
            path=root(uid);state=ex.read(path/'state.json')
            columns=pd.read_csv(path/'upload.csv',nrows=0).columns
            if state.get('append') and str(form.get('name'))!=state['append']:raise ValueError('追加时不能更换数据名称，请另建来源')
            state.update(name=str(form.get('name','')),mapping={c:str(form.get('column_'+str(i),'')) for i,c in enumerate(columns)},amount_unit=str(form.get('amount_unit','')))
            state.pop('checked_hash',None)
            ex.write(path/'state.json',state)
            calendar=form.get('calendar')
            if calendar and getattr(calendar,'filename',''):
                raw=await calendar.read(2_000_001)
                if len(raw)>2_000_000:raise ValueError('交易日历超过 2 MB')
                (path/'calendar.csv').write_bytes(raw)
            validate(path,state)
            state['checked_hash']=selection_hash(path,state)
            ex.write(path/'state.json',state)
            return preview(uid,checked=True)
        except (ValueError,OSError,KeyError) as exc:
            try:
                message=str(exc)
                if '缺少必填列' in message:
                    missing=[LABELS[k] for k in ci.REQUIRED if k not in state['mapping'].values()]
                    message='还缺：'+'、'.join(missing)+'。缺少价格无法计算收益；成交量不能代替成交额。'
                response=preview(uid,message);response.status_code=400;return response
            except (ValueError,OSError,KeyError):return error(exc)

    @app.post('/data/csv/{uid}/save')
    async def save(req:Request,uid:str):
        form=await req.form();guard(req,form.get('token'))
        try:
            path=root(uid);state=ex.read(path/'state.json')
            if form.get('checked_hash')!=state.get('checked_hash') or not state.get('checked_hash') or state['checked_hash']!=selection_hash(path,state):
                raise ValueError('设置已变化，请重新检查数据后保存')
            validate(path,state)
            calendar=(path/'calendar.csv').read_bytes() if (path/'calendar.csv').is_file() else None
            canonical=ci.read(path/'upload.csv',mapping=state['mapping'],amount_unit=state['amount_unit'])
            if state.get('append') or calendar is not None and 'adj_factor' in canonical:
                feed=feed_for(state['append']) if state.get('append') else data_root/state['name']
                daily_data.update(feed,canonical.to_csv(index=False).encode(),calendar)
                ex.write(feed/'csv_mapping.json',{'columns':state['mapping'],'amount_unit':state['amount_unit']})
            else:
                ci.import_csv(path/'upload.csv',data_root,state['name'],state['filename'],mapping=state['mapping'],amount_unit=state['amount_unit'],calendar_raw=calendar)
            project=state.get('project','')
            if project:projects.update(workdir,project,dataset=state['name'])
            if project and projects.read(workdir,project).get('route')=='rules':
                return RedirectResponse('/projects/'+project,303)
            return RedirectResponse('/train?ds='+state['name']+('&project='+project if project else ''),303)
        except (ValueError,OSError,KeyError) as exc:
            try:
                response=preview(uid,str(exc));response.status_code=400;return response
            except (ValueError,OSError,KeyError):return error(exc)
