"""Read-only onboarding and shared explanations; no trading or training policy."""
from __future__ import annotations

import html
import math
from fastapi.responses import RedirectResponse

from . import experiments as ex, strategy, workflow_ui, external_signals, data_readiness, policy, strategy_projects


def esc(value):
    return html.escape(str(value), quote=True)


def modified(path):
    # Workers may publish/replace artifacts while the user opens the workbench.
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


STEPS = (
    ("模型", "训练或选择，给股票打分"),
    ("规则", "按持仓和纪律决定怎么买"),
    ("部署", "设置纸面账户与每日运行"),
    ("运行", "在看板持续跟踪"),
)


def flow(step=0, base="", project=""):
    links = [f"{base}/model", f"{base}/rules", f"{base}/deploy", f"{base}/dashboard"]
    if project:links[0:2]=[f'/projects/{project}/model',f'/projects/{project}/rules']
    parts = []
    for index, (name, meaning) in enumerate(STEPS, 1):
        content = f'<b>{index:02d} · {name}</b>'
        content += '<span class="stage-status">'+('已完成' if index<step else '进行中' if index==step else '未开始')+'</span>'
        attrs = ' aria-current="step"' if index == step else ''
        if (base or project) and (index < step or index == step == 4):
            content = f'<a href="{links[index-1]}">{content}</a>'
        parts.append(f'<li{attrs}>{content}</li>')
    return '<ol class="experience-flow" aria-label="从模型到运行">'+''.join(parts)+'</ol>'


def help_panel():
    parameters = ''.join(f'<dt>{esc(info["label"])} <code>{esc(key)}</code></dt><dd>{esc(info["help"])}</dd>'
                         for key, info in policy.schema()['ui'].items())
    return ('<dialog id="workspace-help" class="workspace-help" aria-labelledby="workspace-help-title">'
            '<header><h2 id="workspace-help-title">帮助与说明</h2><button type="button" id="close-workspace-help" '
            'aria-label="关闭帮助" title="关闭帮助">&times;</button></header>'
            '<details id="help-governance-review"><summary>治理诊断与方案</summary>'
            '<p>输入自己的目标权重、分数或排名，以及实际现金、股数、可卖数量和参考价格。治理诊断不要求先训练模型。</p>'
            '<p>硬约束内比较贴近目标和减少费用的方案；目标偏离是股票与现金权重绝对差之和，1% 从股票移至现金约产生 2 个百分点的合计偏离。</p>'
            '<p>确认只保存参考计划，不修改运行账户，也不发送订单。费用按拟执行日的印花税、输入佣金与滑点假设计价；此入口尚未接 STRUCT、历史节奏优化和未来成交模拟。</p></details>'
            '<details id="help-workflow"><summary>训练与账户规则</summary>'
            '<p>模型给股票打分，账户规则决定持仓数、仓位和调整方式。用默认设置训练，再配置规则、检验历史，最后开启纸面跟踪。</p>'
            '<p>高级设置只控制可见内容，收起不会撤销修改。修改已保存规则会建立新版本，旧账户与记录保留。</p>'
            '<p>LightGBM 和 Ridge 使用 CPU，无需独立显卡。默认 A 股、只做多、5 日预测；目前每日纸面闭环只接入兼容的五日模型。</p>'
            '<p>资金档位用于推导训练持仓和流动性筛选。账户规则按现金加初始持仓的参考权益推导默认值；手动改过的参数保留。</p></details>'
            '<details id="help-import"><summary>导入外部预测</summary><p>只导入分数，不执行外部模型代码。CSV 含 code、decision_time、prediction、generated_at。'
            '两个时间须带时区，每个决策日覆盖完整股票池，生成时间不晚于上海时区 15:30。</p>'
            '<p>同一版本持续追加预测；更换模型版本、股票池或预测期限须新建来源。平台保留首次接收时间，生成时间为来源自报，'
            '导入不代表通过上游前视审计。迟到补传不能改写按时前向记录。</p></details>'
            + data_readiness.REQUIREMENTS.replace('<details class="data-section">', '<details id="help-data">').replace(
                '需要准备什么数据？建议至少 3 年、720 个交易日（点开看详细要求）', '数据准备与字段') +
            '<details id="help-parameters"><summary>参数与模板</summary><p>直接调整、分步调整、波动目标描述不同的交易行为，不代表收益评级。</p>'
            '<p>分步调整每期靠近目标的一部分，退出范围与硬限制优先。默认 0.5 是待验证的工程选择，尚未证明收益优势。'
            '波动目标不是亏损上限；ML 风险辅助仍属实验。</p><p>比例输入使用小数，例如 0.1 = 10%。单票上限超出部分留现金。</p>'
            '<dl>'+parameters+'</dl></details>'
            '<details id="help-evaluation"><summary>历史检验与结果口径</summary>'
            '<p>当前规则与同信号的立即调整、平均实际仓位匹配对照。仓位匹配使用全段历史校准，仅供事后解释，不可部署。</p>'
            '<p>建仓观察期与后段分开报告。尝试次数仅覆盖本地记录；检验完成不表示未来收益优势已经得到证明。</p>'
            '<p>随包入门模型使用公开日线，覆盖 40 只当前上市股票，存在样本选择偏差；不代表历史全市场。模型来源和复现配方可在选择模型页查看。</p></details>'
            '<details id="help-costs"><summary>交易规则与费用</summary>'
            '<p>T+1、申报数量、涨跌停和决策时点固定。默认每五个交易日决策，下一交易日开盘模拟执行；调仓节奏从账户起点计数。</p>'
            '<p>新规则默认计 STRUCT 上月报价半价差估计、佣金及最低佣金、按日印花税和额外滑点压力。'
            'STRUCT 系数拟合于 2021–2023；更早历史属事后成本估计。实际成交冲击未标定，模拟仍可能偏乐观。旧账户保留冻结口径。</p>'
            '<p>建议目标不等于成交；受阻、跳空或价格漂移可能使实际持仓超出目标限制。持仓公司行动和行情缺口会停止记账。</p></details>'
            '<details id="help-deployment"><summary>部署、更新与记录</summary>'
            '<p>部署是本机纸面运行，不连接券商。工作台开启时，启用每日检查后会在 17:30 后每天尝试一次。'
            'BaoStock 先补齐并校验行情，再记账；CSV 行情和外部预测须自行更新。缺失信号不会复制昨日排名。</p>'
            '<p>看板每 30 秒只读账本；刷新显示不会下载行情或触发记账。日频记录不是实时盘口。电脑关闭时任务不运行。</p>'
            '<p>按时纸面、迟到重建与历史演示分开标记。图表净值以全路径首条记录归一为 1；估值价格变化不是持仓浮盈。'
            '最近目标包含价格漂移，不是待执行订单。零成交和部分成交保留在记录中。</p>'
            '<p>完整配置包含账户初值；分享规则模板不含现金、持仓、路径、模型或成绩。对方须用自己的账户重新检验。</p></details>'
            '<p class="mut">结果仅反映给定数据与假设，不代表未来收益。数据、持仓和训练记录保存在本机；统一插件安装仍在规划中。</p>'
            '</dialog>')


def signal_options(workdir):
    """Metadata discovery only; freezing later verifies files and implementation."""
    # Prefer the named latest copy to its archive; both share a run identity.
    paths = [*sorted((workdir / "runs").glob("*/predictions.parquet"), key=modified, reverse=True),
             *sorted((workdir / "runs/history").glob("*/predictions.parquet"), key=modified, reverse=True)]
    found, seen = {}, set()
    for path in paths:
        source = path.parent
        try:
            meta = ex.read(source / "final_model/meta.json")
            record = ex.read(source / "run.json") if (source / "run.json").exists() else {}
            identity = meta.get("run_id") or source.name
            if identity in seen:
                continue
            seen.add(identity)
            alias = source.with_name(record.get("name", source.name))
            if workflow_ui.hidden(workdir, source) or workflow_ui.hidden(workdir, alias):
                continue
            if meta.get('model')=='external_scores':external_signals.validate_meta(meta)
            elif meta.get('model')=='rule_card':
                from . import rule_signals
                rule_signals.validate_model(meta)
            else:strategy.locked_recipe(meta)
            found[source.name] = {"name": record.get('name') or meta.get('name') or source.name, "model": meta["model"],
                                  "data_end": meta["data_end"]}
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return found


def signal_report(metrics):
    """Render recorded signal evidence, separately from account return/cost tests."""
    screen=metrics.get('signal_screen') or {}
    label='有历史增量证据' if screen.get('status')=='PASS' else '暂不能判断'
    body='<h2>排序能力 · '+label+'</h2>'
    if metrics.get('data_snapshot_id')=='snap-synthetic':body+='<p class="warn">这是试运行数据，没有投资含义。</p>'
    model_key=metrics.get('model_key','lgbm_ensemble')
    if model_key == 'rule_card':
        body += f'<p class="policy-meta">历史开发分析 · {esc(metrics.get("rule_attempts",1))} 次规则检验 · 尚未证明净收益优势</p>'
    rows=metrics.get('ic',{})
    body+='<div class="tw"><table><tr><th>信号</th><th>RankIC</th><th>t 值</th><th>日数</th></tr>'
    def number(value):
        return f'{value:+.3f}' if isinstance(value,(int,float)) and math.isfinite(value) else '样本不足'
    for name,row in rows.items():
        if name.startswith('lgbm_seed'):continue
        stat=row.get('final_test',{})
        body+=f'<tr><td>{"当前模型" if name==model_key else esc(name)}</td><td>{number(stat.get("mean"))}</td><td>{number(stat.get("t"))}</td><td>{esc(stat.get("n",0))}</td></tr>'
    body+='</table></div>'
    if model_key == 'rule_card':
        coverage=metrics.get('coverage',{})
        body+=f'<p class="policy-meta">有效输入 {esc(coverage.get("valid_cells",0))} / 候选样本 {esc(coverage.get("universe_cells",0))} · 最少历史 {esc(coverage.get("minimum_history_sessions",60))} 个交易日 · 缺失日期 {esc(coverage.get("invalid_days",0))}</p>'
        prior=metrics.get('paired_previous')
        if prior:
            diff=prior['rank_ic_difference']
            body+=f'<p>与上一版配对排序差 {number(diff.get("mean"))} · {esc(diff.get("n",0))} 日 · 暂不能据此确认收益改善</p>'
            body+=f'<p class="policy-meta">当前 / 上版出分样本 {esc(prior["candidate_cells"])} / {esc(prior["reference_cells"])}；条件与历史窗口改变候选范围的影响包含在比较中。</p>'
        if metrics.get('rule_changes'):
            body+='<details><summary>查看规则变更</summary><ul>'+''.join(
                f'<li>{esc(change["before"])} → {esc(change["after"])}</li>' for change in metrics['rule_changes'])+'</ul></details>'
        body+='<details><summary>规则来源与检验范围</summary><ul>'+''.join(
            f'<li>{esc(r["description"])} · {esc(r["origin"])}</li>' for r in metrics.get('rule_card',{}).get('rules',[]))+'</ul><p>逐条经济贡献未分析。修改后的历史不属于新的独立留出。</p></details>'
    comparisons=screen.get('comparisons',{})
    if comparisons:
        body+='<details><summary>与简单方法的配对差值</summary><div class="tw"><table><tr><th>对照</th><th>RankIC 差值</th><th>区间（预设对照族校正）</th><th>结论</th></tr>'
        labels={'IMPROVED':'本段有增量','DEGRADED':'本段较弱','NO_CHANGE':'本段相同','INCONCLUSIVE':'暂不能判断','INSUFFICIENT':'样本不足'}
        for name,stat in comparisons.items():
            interval=stat.get('ci95_family')
            body+=f'<tr><td>{esc(name)}</td><td>{number(stat.get("mean"))}</td><td>'+(' 至 '.join(number(v) for v in interval) if interval else '样本不足')+f'</td><td>{labels.get(stat.get("status"),"暂不能判断")}</td></tr>'
        body+='</table></div><p class="mut">仅历史信号比较，未校正全部调参搜索，不是前向收益认证。</p></details>'
    views=(metrics.get('ledger') or {}).get('views')
    if views is not None:body+=f'<p class="policy-meta">本地测试期查看 {esc(views)} 次</p>'
    return body


def project_index(workdir):
    """Discover resumable strategy versions and drafts without mutating them."""
    projects, unreadable = [], 0
    for identity in (workdir / "runs/strategies").glob("*/identity.json"):
        try:
            versions = sorted(identity.parent.glob("versions/*/spec.json"), key=modified, reverse=True)
            for index, version in enumerate(versions):
                root = version.parent
                running = (root / "runtime.json").exists()
                if index and not running:
                    continue
                spec = ex.read(version)
                runtime = ex.read(root / "runtime.json") if running else {}
                health = ex.read(root / "health.json") if (root / "health.json").exists() else {}
                demo = runtime.get("scope") == "historical_replay"
                evaluated = any(ex.read(p).get('status') == 'SUCCEEDED' for p in root.glob('evaluations/*/result.json'))
                projects.append({"name": spec["name"], "url": f'/strategies/{spec["sid"]}/{spec["vid"]}' + ('/dashboard' if running else '/deploy' if evaluated else ''),
                    "priority": 2 if running and not demo else 1 if running else 0,
                    "label": "历史演示" if demo else "运行中" if running else "规则检验完成 · 待部署" if evaluated else "规则 · 待检验",
                    "message": health.get("message", "模型已确认 · 规则待检验 · 部署未开始 · 运行未开始"), "updated": modified(version)})
        except (ValueError, OSError, KeyError, TypeError):
            unreadable += 1
    for path in (workdir / 'runs/strategy_drafts').glob('*.json'):
        try:
            draft = strategy_projects.resolved(workdir, path.stem)
            if draft.get('version'):
                continue
            projects.append(dict(name=draft['name'], url='/projects/'+draft['id'], priority=0,
                label='数据 · '+{'queued':'排队中','running':'下载中','failed':'下载失败','cancelled':'已取消','succeeded':'已就绪，待继续'}.get(draft.get('data_job_status'),'准备中') if draft.get('data_job') else '规则 · 待保存' if draft.get('stage')=='rules' else '模型 · '+('训练'+{'queued':'排队中','running':'运行中','failed':'失败','cancelled':'已取消','succeeded':'完成'}.get(draft.get('job_status'),'待训练') if draft.get('job') else '待确认'),
                message='模型 → 规则 → 部署 → 运行', updated=modified(path)))
        except (ValueError, OSError, KeyError, TypeError):
            unreadable += 1
    return sorted(projects, key=lambda row: row['updated'], reverse=True), unreadable


def register(app, workdir, *, page, datasets, token):
    @app.get("/")
    def home():
        return RedirectResponse('/strategy-governance', 303)

    @app.get("/start")
    def start(ds:str='',next_signal:str=''):
        if ds or next_signal:
            from urllib.parse import urlencode
            return RedirectResponse('/train?'+urlencode({'ds':ds,'next_signal':next_signal}),303)
        projects, unreadable = project_index(workdir)
        body = '<div class="page-heading"><h1>我的策略</h1></div>'
        body += '<section class="review-entry"><h2>已有预测、持仓和交易规则</h2><a class="btn primary" href="/review/new">开始治理诊断</a></section>'
        body += '<section class="experience-paths" aria-label="新建策略">'
        body += '<article><h2>从你的经验开始</h2><p class="mut">趋势 · 反转 · 低波 · 量价</p><a class="btn primary" href="/playbooks">选择起步模板</a></article>'
        for route, title, label in (('train','自己训练一个模型','开始训练'),('select','用现成的模型','选择模型')):
            body += f'<article><h2>{title}</h2><form method="post" action="/projects"><input type="hidden" name="token" value="{token}"><input type="hidden" name="route" value="{route}"><button>{label}</button></form></article>'
        body += '</section><p class="mut">还没有数据？ <a href="/train#free-data">下载免费日线</a> · <a href="/template.csv">下载 CSV 模板</a></p>'
        if projects:
            body += '<section class="experience-panel"><h2>继续上次的工作</h2><div class="experience-projects">'
            for project in projects:
                body += f'<a class="experience-project" href="{esc(project["url"])}"><span class="tag">{project["label"]}</span><strong>{esc(project["name"])}</strong><small>{esc(project["message"])}</small><span>打开策略 →</span></a>'
            body += '</div></section>'
        if unreadable:
            body += '<p class="warn">部分策略元数据无法读取，请在“我的策略”中检查；其他项目仍可使用。</p>'
        return page("我的策略", body)
