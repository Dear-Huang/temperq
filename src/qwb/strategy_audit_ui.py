"""Import once, inspect evidence, then run replaceable strategy audit modules."""
from __future__ import annotations

import io
import math
import zipfile

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.datastructures import FormData, QueryParams

from .securities import asset_class
from . import experiments as ex, strategy_audit as audit, strategy_audit_store as store
from . import strategy_imports as imports
from .audit_catalog import coverage, STAGES
from . import audit_input
from . import audit_period
from . import audit_materials as materials
from . import audit_code, audit_verdicts, audit_actions, audit_attribution, audit_verification
from .governance_review_ui import esc, money, pct, csv_text, form_values, editor

CHECK_NAMES = {"equity_identity": "权益 = 现金 + 市值", "market_value_total": "逐股市值合计",
               "position_valuation": "股数 × 收盘价", "fill_notional": "成交金额",
               "inventory": "库存变动及公司行动", "cash_flow": "现金变动及费用",
               "pnl_bridge": "逐股盈亏与权益变动", "prior_inventory_for_sales": "卖出库存必要条件"}

TABS = (('governance', '/strategy-governance', '策略治理'),
        ('attribution', '/strategy-attribution', '盈亏归因'),
        ('comparison', '/strategy-comparison', '效果验证'))
# Paper trading is kept reachable for existing users but is not part of the 1.0 navigation.
SIMULATION_PATH = '/strategy-simulation'


def display_name(record):
    return f'{record.get("platform", "导入平台")} · {record["name"]} · v{record.get("version", 1)}'


def navigation(workdir, identity='', tab='governance'):
    body = '<nav class="nav" aria-label="主导航">'
    for key, path, label in TABS:
        link = path + ('?strategy='+identity if identity else '')
        body += f'<a href="{link}"'+(' class="on" aria-current="page"' if key == tab else '')+'>'+label+'</a>'
    body += '<a href="/settings" aria-label="设置">⚙</a></nav>'
    body += '<div class="strategy-actions"><a href="/strategy-audits">我的策略</a><a class="btn primary" href="/strategy-audits/new">策略导入</a></div>'
    return body


def strategy_picker(workdir, identity='', tab='governance'):
    action = next((path for key, path, _ in TABS if key == tab), SIMULATION_PATH if tab == 'simulation' else '/strategy-governance')
    body = f'<form class="strategy-picker" action="{action}" method="get"><label for="current-strategy">当前策略</label><select id="current-strategy" name="strategy" onchange="this.form.requestSubmit()"><option value="">选择策略</option>'
    records = store.history(workdir, latest_only=True)
    if identity and all(r['id'] != identity for r in records):
        records.insert(0, store.read(workdir, identity)[0])
    for record in records:
        selected = ' selected' if record['id'] == identity else ''
        body += f'<option value="{record["id"]}"{selected}>{esc(display_name(record))}</option>'
    body += '<option value="new">+ 导入新策略</option></select><noscript><button>切换</button></noscript></form>'
    return body


def help_panel():
    return ('<dialog id="workspace-help" class="workspace-help" aria-labelledby="workspace-help-title">'
            '<header><h2 id="workspace-help-title">帮助与说明</h2><button type="button" id="close-workspace-help" '
            'aria-label="关闭帮助" title="关闭帮助">&times;</button></header>'
            '<details id="help-workflow"><summary>审计范围与结论</summary>'
            '<p>审计从用户的预测交付开始，覆盖目标转成组合、调整、执行、对账、归因和授权留痕。'
            '逐项显示基本通过、缺乏证据或需要调整，并说明已检查的范围，不汇总成策略好坏总分。</p></details>'
            '<details id="help-import"><summary>平台材料与时点</summary><p>同一次回测的交易、持仓、日志、代码和归因可一次导入。'
            '代码只静态解析，参数候选不等于已生效规则。没有声明空仓期初时，以首个收盘账户为基线。</p></details>'
            '<details id="help-governance-review"><summary>账户方案与约束</summary><p>目标、持仓、可卖股数、现金、价格和交易限制共同确定可行方案。'
            '约束冲突不会自动放宽，确认只保存计划，不下单也不写入成交。历史期末快照需要更新后才能当作当前账户。</p></details>'
            '<details id="help-costs"><summary>费用与归因口径</summary><p>逐股净贡献包含已平仓和未实现盈亏。'
            '盈利集中度不等于预测能力；最低佣金情景不是已实现节省；平台行业归因需先核对日期和方法。</p></details>'
            '<p class="mut">数据与证据保存在本机；结果仅覆盖所声明的输入、规则和价格条件。</p></dialog>')


def heading(title, link="/strategy-audits", label="策略审计记录"):
    return f'<link rel="stylesheet" href="/static/strategy-audit.css"><div class="review-heading"><div><p class="review-kicker">TemperQ / 策略审计</p><h1>{esc(title)}</h1></div><a href="{link}">{label}</a></div>'


def workflow(identity='', current='import'):
    if current == 'attribution':
        current = 'governance'
    steps = [('import', '材料确认', f'/strategy-audits/{identity}' if identity else '/strategy-audits/new'),
             ('governance', '诊断与改法', f'/strategy-governance?strategy={identity}'),
             ('comparison', '回测复验', f'/strategy-comparison?strategy={identity}'),
             ('simulation', '账户方案', f'/strategy-simulation?strategy={identity}')]
    body = '<nav class="audit-workflow" aria-label="策略处理流程"><ol>'
    for index, (key, label, url) in enumerate(steps, 1):
        active = ' aria-current="step"' if key == current else ''
        text = f'<span>{index:02d}</span><b>{label}</b>'
        body += f'<li{active}>' + (f'<a href="{url}">{text}</a>' if identity or key == 'import' else f'<div>{text}</div>') + '</li>'
    return body + '</ol></nav>'


def workspace_summary(result):
    modules = result['modules']
    account, attr = modules.get('accounting'), modules.get('attribution')
    if not account or not attr:
        return ''
    state = '已核对' if account['status'] == 'verified' else '待核对 · 归因暂算'
    metrics = [('账户账目', state), ('归因期间', str(len(account['daily']))),
               ('区间净盈亏', '¥'+money(attr['net_pnl'])), ('已扣费用', '¥'+money(attr['fees']))]
    return '<div class="audit-workspace-metrics">'+''.join(f'<div><span>{label}</span><strong>{value}</strong></div>' for label, value in metrics)+'</div>'


def sample_files():
    """A synthetic two-stock lifecycle with a closed holding and cash dividend."""
    tables = {
        "trades.csv": "日期,时间,证券代码,买/卖,数量,成交价,成交金额,交易费用\n2024-01-03,09:30,600000.SH,卖出,100,11,1100,6.1\n2024-01-02,09:30,600000.SH,买入,100,10,1000,5\n,09:30,000001.SZ,买入,100,20,2000,5\n",
        "holdings.csv": "日期,证券代码,数量,持仓均价,收盘价,持仓市值\n2024-01-03,000001.SZ,100,20,21,2100\n,,,总资产: 10183.9,现金: 8083.9,2100\n2024-01-02,600000.SH,100,10,10,1000\n,000001.SZ,100,20,20,2000\n,,,总资产: 9990,现金: 6990,3000\n",
        "logs.csv": "时间,级别,内容\n2024-01-03,INFO,\"split[2024-01-03][000001.SZ,old=100,now=100,cash=0]\"\n",
        "strategy.py": "def initialize(context):\n    context.stock_count = 2\n    context.max_cash_per_instrument = 0.5\n# Synthetic export only\n",
    }
    return [imports.SourceFile(name, text.encode("utf-8")) for name, text in tables.items()]


def upload_form(token, error=""):
    body = heading("策略导入", "/strategy-audits", "我的策略")
    body += workflow()
    if error:
        body += f'<p role="alert" class="notice warn">{esc(error)}</p>'
    body += f'''<form class="review-form" method="post" action="/strategy-audits/import" enctype="multipart/form-data">
<input type="hidden" name="token" value="{token}">
<section><h2>01 · 平台材料</h2><div class="review-fields">
<label>策略名称<input name="name" maxlength="80" value="我的策略审计" required></label>
<label>导出平台<select name="adapter"><option value="auto">自动识别</option><option value="bigquant">BigQuant</option><option value="joinquant">聚宽</option><option value="ricequant">米筐</option><option value="generic">其他 / 标准 CSV</option></select></label>
<label>风格（可留空，由你确认）<input name="style" maxlength="40" placeholder="例如短期反转、趋势、多因子"></label>
</div><p class="notice">推荐至少 2 年，最好 3 年以上，并覆盖至少一段明显下跌。较短回测也可导入，报告会区分可核账目与规则效果。</p>
<label>原样上传代码、交易和可选持仓、日志、每日打分；平台下载的 ZIP 不用解压（最多16个文件）<input type="file" name="files" accept=".csv,.txt,.py,.zip,.json,.xlsx" multiple></label>
<details><summary>也可以直接粘贴策略代码</summary><label>策略代码<textarea name="code_text" rows="7" maxlength="200000" placeholder="只读解析，不运行代码；已上传代码文件时不必再粘贴"></textarea></label></details>
<p class="mut">代码必填；仅有代码时可做静态检查。交易必填，聚宽持仓可推算净变动；持仓强烈推荐、日志推荐、每日打分选填。支持 UTF-8 / GBK，不用改列名。</p>
<details open class="platform-guide"><summary>各平台怎么导出、能拿到什么</summary>
<div class="tw"><table><tr><th>平台</th><th>怎么导出</th><th>费用与价差</th><th>盈亏归因</th><th>生成优化代码</th></tr>
<tr><td>BigQuant</td><td>回测结果页导出交易、持仓、日志，加策略代码，原样上传</td><td>能</td><td>能</td><td>能</td></tr>
<tr><td>聚宽</td><td>研究环境运行 <a href="/static/joinquant_export.py" download>导出脚本</a>，上传生成的 temperq_*.csv 和策略代码；也可只传回测持仓表</td><td>能（需逐笔成交）</td><td>能</td><td>暂不提供，给文字建议</td></tr>
<tr><td>其他 Python 平台</td><td>按通用格式整理 trades.csv、holdings.csv、account.csv</td><td>能</td><td>能</td><td>不提供</td></tr>
<tr><td>公式语言平台</td><td>按通用格式整理；代码只存档，不解析</td><td>能</td><td>能</td><td>不提供</td></tr></table></div>
<p class="mut">通用格式：trades.csv 列为 date,time,code,side,quantity,price,fee,order_id（time、order_id 可空）；holdings.csv 为 date,code,quantity,price；account.csv 为 date,equity,cash；每日打分 scores.csv 为 date,code,score,available_at。成交价建议用不复权真实价。</p></details>
<p class="mut">可选报价表列：date, code, quoted_spread_bp, sample_count, measurement_scope。最后一列固定为 daily_quote_mean，表示日均完整报价价差（bp），不是实测成交成本。</p>
</section><section><h2>02 · 期初账户</h2>
<label class="review-check"><input type="checkbox" name="opening_empty" value="yes">确认导出从空仓期初开始，期间无外部出入金</label>
<label>期初现金（元）<input name="opening_cash" type="number" min="0.01" step=".01" placeholder="可留空"></label>
</section><div class="review-submit"><button class="primary">导入并核对材料</button><a href="/strategy-audits/sample.zip">下载合成示例</a></div></form>'''
    body += '<section class="review-section"><h2>材料格式模板</h2><p class="mut">原平台文件可直接上传；模板只提供字段与说明，不包含虚构数据。先选导出平台即可下载对应模板；自动识别时提供标准CSV。BigQuant与聚宽的账户汇总保留在原持仓表中。</p><div class="material-template-links">'
    for kind in ('code', 'fills', 'positions', 'snapshots', 'logs', 'scores'):
        body += f'<a data-template-kind="{kind}" href="/strategy-audits/templates/{kind}.zip">下载{materials.MATERIALS[kind][0]}模板</a>'
    body += '</div></section><script>(function(){var s=document.querySelector("select[name=adapter]");function update(){document.querySelectorAll("[data-template-kind]").forEach(function(a){a.href="/strategy-audits/templates/"+a.dataset.templateKind+".zip?adapter="+(s.value==="auto"?"generic":s.value);a.hidden=a.dataset.templateKind==="snapshots"&&["bigquant","joinquant"].includes(s.value)})}s.addEventListener("change",update);update()})();</script>'
    return body


def premise_fields(bundle, premise=None):
    """The idea the strategy rests on; checked against the backtest in 盈亏归因."""
    from . import audit_premise
    premise = premise or {}
    scores = bundle.get('scores') or []
    guess = audit_premise.infer_direction(scores) if scores else None
    code = audit_premise.from_code(bundle)
    factors = '、'.join(f['name'] for f in (code or {}).get('factors', [])[:3])
    direction = premise.get('direction') or ''
    options = ''.join(f'<option value="{k}"{" selected" if direction == k else ""}>{v}的标的，随后涨得更多</option>'
                      for k, v in audit_premise.DIRECTIONS.items())
    hint = ('已导入策略打分：' + (f'从选股行为看，策略选的是{audit_premise.DIRECTIONS[guess]}的标的。' if guess else '未能从选股行为判断方向。')
            if scores else '未导入策略打分：盈亏归因里只能记录前提，不能检验。上传每日打分（date, code, score）和候选池收盘价（date, code, close）后可检验。')
    return ('<details class="import-details" open><summary>策略前提（你认为这个策略为什么能赚钱）</summary>'
            f'<p class="mut">{esc(hint)}' + (f'代码里识别到的因子：{esc(factors)}。' if factors else '') + '留空时按代码和选股行为识别。</p><div class="review-fields">'
            f'<label>前提（一句话）<input name="premise_text" maxlength="200" value="{esc(premise.get("text", ""))}" '
            'placeholder="例如：过去20天涨得多的股票，下周还会继续跑赢"></label>'
            f'<label>分数方向<select name="premise_direction"><option value="">按选股行为识别</option>{options}</select></label>'
            '</div></details>'
            '<details class="import-details"><summary>帖子或宣传里声称的收益（选填）</summary>'
            '<p class="mut">填了之后，报告会把声称收益和同一份材料复算的收益放在一起对照。</p><div class="review-fields">'
            '<label>声称累计收益（%）<input name="claimed_total" inputmode="decimal" placeholder="例如 520"></label>'
            '<label>声称年化收益（%）<input name="claimed_annual" inputmode="decimal" placeholder="例如 59"></label>'
            '<label>出处<input name="claim_source" maxlength="200" placeholder="例如 帖子标题"></label></div></details>')


def threshold_fields(values=None):
    """判断口径: experience thresholds only. Exchange rules, tax rates and identities are not listed."""
    from . import audit_rules
    values = {**audit_rules.defaults(), **(values or {})}
    percent = lambda r: r.unit == "比例"
    rows = ''
    for rule in audit_rules.RULES.values():
        if rule.kind != 'threshold':
            continue
        low, high = rule.range
        scale = 100 if percent(rule) else 1
        unit = '%' if percent(rule) else rule.unit
        step = '0.1' if percent(rule) or isinstance(rule.value, float) else '1'
        assets = '、'.join(audit_rules.ASSET_NAMES[a] for a in rule.applies_to)
        value = values[rule.id] * scale
        default = rule.value * scale
        changed = ' class="threshold-changed"' if abs(value - default) > 1e-9 else ''
        rows += (f'<label{changed}><span>{esc(rule.text)}（{esc(unit)}）</span>'
                 f'<input type="number" name="t:{rule.id}" value="{value:g}" min="{low * scale:g}" max="{high * scale:g}" '
                 f'step="{step}" data-default="{default:g}">'
                 f'<small class="mut">{esc(rule.id)} · 默认 {default:g}{esc(unit)} · 范围 {low * scale:g}–{high * scale:g} · {esc(assets)}</small></label>')
    return ('<details class="import-details threshold-settings"><summary>判断口径（经验阈值，可调）</summary>'
            '<p class="mut">这些是项目自己定的判断标准，尚未经样本检验；改了会生成新报告，旧报告不变。'
            '交易所规则、税率和账务恒等式不在这里，不能改。<a href="/strategy-audits/rules">查看全部规则</a></p>'
            f'<div class="review-fields">{rows}</div>'
            '<button type="button" class="link" onclick="this.closest(\'details\').querySelectorAll(\'input[data-default]\')'
            '.forEach(function(i){i.value=i.dataset.default})">全部恢复默认值</button></details>')


def threshold_values(form):
    """Read 判断口径 back; only values that differ from the defaults are stored as overrides."""
    from . import audit_rules
    out = {}
    for rule in audit_rules.RULES.values():
        raw = form.get('t:' + rule.id)
        if rule.kind != 'threshold' or raw in (None, ''):
            continue
        value = float(raw) / (100 if rule.unit == '比例' else 1)
        if abs(value - rule.value) > 1e-12:
            out[rule.id] = value
    return out


def overview(identity, bundle, token, inherited=None):
    body = heading('策略导入 · 核对与补齐', '/strategy-audits', '我的策略')
    body += workflow(identity)
    body += '<p>'+esc(bundle['name'])+' · 完成以下核对后，进入四页共用的策略工作区。</p>'
    days = bundle["snapshots"]
    review = audit_input.review(bundle)
    period = f'{review["actual_start"]} 至 {review["actual_end"]}' if review['actual_start'] else '仅代码，静态检查'
    fill_label = '条持仓净变动（由持仓推算）' if review['fills_inferred'] else '笔成交'
    body += f'<p class="review-meta">{period} · {len(days)} 个账户快照 · {len(bundle["fills"])} {fill_label}</p>'
    body += '<section class="review-section"><h2>01 · 材料核对</h2><div class="tw"><table><tr><th>文件</th><th>识别类型</th><th>行数</th></tr>'
    labels = {"fills": "交易", "positions": "持仓与账户汇总", "snapshots": "账户汇总", "logs": "日志", "code": "策略代码", "external_attribution": "平台归因（待核口径）", "quote_observations": "日均报价价差（核对用途）", 'scores': '每日打分', 'platform_returns': '平台收益表（核对用途）', 'orders': '委托详情', 'auxiliary': '辅助表（不参与审计）', 'platform_summary': '平台绩效汇总（核对用途）', 'prices': '候选池收盘价', 'minute_prices': '成交日分钟价', 'day_ranges': '成交日全天区间', 'engine_config': '回测运行配置', 'empty': '空表（无数据）'}
    for source in bundle["sources"]:
        body += f'<tr><td>{esc(source["name"])}</td><td>{labels[source["kind"]]} · {esc(source.get("encoding", "历史记录"))}</td><td>{source["rows"]}</td></tr>'
    body += '</table></div>'
    if bundle["opening"]["kind"] == "empty":
        body += f'<p>期初：空仓，现金 ¥{money(bundle["opening"]["cash"])}（用户确认）</p>'
    else:
        body += '<p class="notice">未声明空仓期初，以首个收盘账户为基线；首日成交不计入后续盈亏归因。外部出入金须另行核对。</p>'
    body += '</section>'+input_checks(review, identity)+material_editor(identity, bundle, token)
    body += '<details class="import-details"><summary>回测长度、可审范围与内部验证分段</summary>'+period_section(audit_period.assess(bundle))+'</details><section class="review-section"><h2>02 · 确认并完成导入</h2>'
    body += f'<form method="post" action="/strategy-audits/{identity}/analyze" class="review-form"><input type="hidden" name="token" value="{token}"><input type="hidden" name="input_digest" value="{ex.object_digest(bundle)}"><div class="review-fields">'
    prefills = {p['key']: p['value'] for p in review['parameters']}
    if inherited:
        # A re-run version keeps the parent report's cost scenario, so both versions are restated alike.
        prefills.update({k: inherited[k] for k in ('commission_bp', 'minimum_commission', 'max_weight') if k in inherited})
        body += (f'<p class="notice">这是上一版的改版：费用情景沿用上一版报告（佣金 {inherited["commission_bp"]:g}bp、'
                 f'最低佣金 ¥{inherited["minimum_commission"]:g}），两版按同一口径比较。改了这里，比较就不再同口径。</p>')
    for key, label, value, mini, maxi in (("commission_bp", "佣金情景（bp）", prefills['commission_bp'], 0, 1000), ("minimum_commission", "最低佣金情景（元）", prefills['minimum_commission'], 0, 10000), ("max_weight", "单票收盘上限（%）", prefills['max_weight'] * 100, .01, 100)):
        body += f'<label>{label}<input type="number" name="{key}" value="{value}" min="{mini}" max="{maxi}" step=".01" required></label>'
    checked = ' checked' if inherited and inherited.get('broker_confirmed') else ''
    body += (f'<label class="check"><input type="checkbox" name="broker_confirmed" value="yes"{checked}>'
             '上面的佣金和最低佣金是我券商合同的费率（不勾选时只作费率情景，不判断回测成本偏高或偏低）</label>')
    body += premise_fields(bundle, (inherited or {}).get('premise'))
    body += threshold_fields((inherited or {}).get('thresholds'))
    body += '<input type="hidden" name="destination" value="workspace">'
    button = '完成导入，进入策略治理'
    disabled = '' if review['can_analyze'] else ' disabled'
    body += '</div><p class="mut">从代码读出的值已预填；字面量可能处于未执行分支，确认后仅用于本报告。税率按日期、最小价位和涨跌停规则锁定。价位半档为主情景，竞价成本不能由半档认证。</p>'
    body += '<details id="audit-advanced"><summary>高级设置</summary><p class="mut">以下按工程默认值设置，一般不用改。默认值不表示已在你的策略上证明最优。</p><div class="review-fields"><label>内部验证比例（%）<input type="number" name="validation_fraction" min="10" max="50" step="1" value="25"></label><label>规则最少触发及独立日期数<input type="number" name="minimum_triggers" min="20" max="10000" value="20"></label><label>边界隔离（观测交易日期）<input type="number" name="purge_days" min="1" max="252" value="20"></label></div><p class="mut">验证段至少3个月，隔离边界后建议段也至少3个月。20次只允许进入效果检验，不能直接判定有效。</p></details>'
    body += '<p class="mut">选填材料可稍后补充；按现有材料生成结果，不把缺项当作已审。仅有代码时完成静态检查。</p><label class="review-check"><input type="checkbox" name="same_run" value="yes" required>确认这些文件来自同一次回测，已核对上述参数</label><button class="primary"'+disabled+'>'+button+'</button></form></section>'
    body += '<details class="import-details" id="strategy-parameters"><summary>策略参数与代码来源明细</summary><div class="tw"><table><tr><th>参数</th><th>识别值</th><th>来源</th></tr>'
    origins = {'code_literal': '代码候选，待确认', 'conflicting_literals': '多个冲突候选，需核对', 'platform_default': '平台默认', 'missing': '未读出', 'needs_confirmation': '需补充'}
    for p in review['parameters']:
        body += f'<tr><td>{esc(p["label"])}</td><td>{esc(p["value"]) if p["value"] is not None else "待补充"}</td><td>{origins[p["source"]]}</td></tr>'
    body += '</table></div><p class="mut">hold_days 可能只参与分批建仓，未确认前不解释为强制退出期限。风格由用户确认，不从持仓收益自动判断策略风格。</p>'
    code = bundle["code"]
    if code:
        body += '<section class="review-section"><h2>代码中识别的参数候选</h2><p class="mut">'+esc(code["note"])+ '</p><div class="tw"><table><tr><th>行号</th><th>参数</th><th>字面值</th></tr>'
        for row in code["candidates"]:
            body += f'<tr><td>{row["line"]}</td><td>{esc(row["key"])}</td><td class="audit-literal">{esc(row["value"])}</td></tr>'
        body += '</table></div></section>'
    return body+'</details>'


def input_checks(review, identity=''):
    labels = {'consistent': '一致', 'blocked': '需补充', 'review': '请核对', 'approximate': '近似', 'missing': '未审 / 缺材料'}
    body = '<section class="review-section" id="input-consistency"><h2>导入一致性核对</h2>'
    checks = sorted(review['checks'], key=lambda item: item['status'] == 'consistent')
    passed = [item for item in checks if item['status'] == 'consistent']
    optional = {'scores', 'fee_units', 'orders_vs_fills', 'price_basis'}
    pending = [c for c in checks if c['status'] != 'consistent']
    urgent = [c for c in pending if not (c['status'] == 'missing' and c['key'] in optional)]
    later = [c for c in pending if c not in urgent]
    if not pending:
        body += '<p>全部核对通过。</p>'
    for item in urgent + later:
        if item is (later[0] if later else None):
            body += f'<details class="import-details" id="optional-materials"><summary>选填材料：可稍后补充（{len(later)} 项，不影响开始审计）</summary>'
        body += f'<article class="audit-finding"><h3>{esc(item["title"])} <span class="tag">{labels[item["status"]]}</span></h3><p>{esc(item["detail"])}</p><p class="mut">{esc(item["action"])}</p></article>'
        if identity:
            kinds = materials.CHECK_MATERIALS.get(item['key'], ())
            body += '<div class="material-actions">'
            for kind in kinds:
                if kind == 'snapshots' and review['platform'] in {'BigQuant', '聚宽'}:
                    continue
                body += material_links(identity, kind)
            body += '</div>'
    if later:
        body += '</details>'
    if passed:
        body += f'<details class="import-details"><summary>已通过 {len(passed)} 项核对</summary>'
        for item in passed:
            body += f'<p><b>{esc(item["title"])}</b> · {esc(item["detail"])}</p>'
        body += '</details>'
    return body+'</section>'


def material_links(identity, kind):
    label = materials.MATERIALS[kind][0]
    return (f'<a class="btn" href="/strategy-audits/{identity}#material-{kind}">上传{label}</a>'
            f'<a href="/strategy-audits/{identity}/templates/{kind}.zip">下载{label}模板</a>')


def material_editor(identity, bundle, token):
    kinds = {s['kind'] for s in bundle['sources']}
    body = '<section class="review-section" id="import-materials"><h2>补充或替换材料</h2><p class="mut">只上传要补的文件，其余材料自动沿用。同类文件整份替换，保存为新材料版本；需要再次确认，旧报告保持原样。</p>'
    for kind, (label, hint) in materials.MATERIALS.items():
        if kind == 'snapshots' and bundle['adapter'] in {'bigquant_v1', 'joinquant_v1', 'ricequant_v1'}:
            continue
        if kind == 'quote_observations' and bundle['adapter'] in {'generic_v1', 'joinquant_v1'}:
            continue
        present = kind in kinds
        body += f'<details class="material-editor" id="material-{kind}"><summary>{label} · '+('已上传，可替换' if present else '待补充')+'</summary>'
        body += f'<p>{hint}</p><form class="review-form material-upload" method="post" action="/strategy-audits/{identity}/materials" enctype="multipart/form-data"><input type="hidden" name="token" value="{token}"><input type="hidden" name="input_digest" value="{ex.object_digest(bundle)}"><input type="hidden" name="kind" value="{kind}">'
        body += f'<label>选择{label}文件<input type="file" name="files" accept="'+('.py,.txt' if kind == 'code' else '.csv')+'"></label>'
        if kind == 'code':
            body += '<label>或粘贴完整策略代码<textarea name="code_text" rows="6" maxlength="200000"></textarea></label>'
        body += '<div class="material-actions"><button>上传并重新核对</button>'+f'<a href="/strategy-audits/{identity}/templates/{kind}.zip">下载{label}模板</a></div></form></details>'
    body += '</section><script>(function(){function show(){var d=document.getElementById(location.hash.slice(1));if(d&&d.matches("details.material-editor")){d.open=true;d.scrollIntoView({block:"start"})}}addEventListener("hashchange",show);show()})();</script>'
    return body


async def upload_fields(req):
    async def bounded_stream():
        size = 0
        async for chunk in req.stream():
            size += len(chunk)
            if size > imports.MAX_TOTAL_BYTES + 1_000_000:
                raise MultiPartException('上传请求不能超过 25MB')
            yield chunk
    try:
        if req.headers.get('content-type', '').startswith('application/x-www-form-urlencoded'):
            raw = b''.join([chunk async for chunk in bounded_stream()])
            if len(raw) > 1_000_000:
                raise MultiPartException('表单内容过长')
            pairs = QueryParams(raw.decode('utf-8')).multi_items()
            if len(pairs) > 12:
                raise MultiPartException('表单字段过多')
            return FormData(pairs)
        return await MultiPartParser(req.headers, bounded_stream(), max_files=imports.MAX_FILES + 2, max_fields=12,
                                     max_part_size=imports.MAX_FILE_BYTES).parse()
    except (MultiPartException, UnicodeDecodeError) as exc:
        raise HTTPException(400, str(exc)) from None


async def uploaded_files(form):
    files, total = [], 0
    for upload in form.getlist('files'):
        if not hasattr(upload, 'read'):
            raise ValueError('请上传文件')
        raw = await upload.read(imports.MAX_FILE_BYTES + 1)
        if not upload.filename and not raw:
            continue
        total += len(raw)
        if len(raw) > imports.MAX_FILE_BYTES or total > imports.MAX_TOTAL_BYTES:
            raise ValueError('单文件最多 8MB，合计最多 24MB')
        files.append(imports.SourceFile(upload.filename, raw))
    if str(form.get('code_text', '')).strip():
        files.append(imports.SourceFile('pasted_strategy.py', str(form['code_text']).encode('utf-8')))
    return files


def period_section(period):
    split = period['partition']
    body = '<section class="review-section" id="audit-period"><h2>回测长度与内部验证</h2><p class="notice">'+esc(period['label'])+'</p>'
    body += '<p>'+esc(period['rules_effect_warning'])+'</p>'
    if split['status'] == 'planned':
        body += f'<div class="audit-split"><div><span>建议段</span><strong>{split["development_start"]} 至 {split["development_end"]}</strong></div><div><span>边界隔离</span><strong>{len(split["purged_dates"])} 个观测日期</strong></div><div><span>内部验证段</span><strong>{split["validation_start"]} 至 {split["validation_end"]}</strong></div></div>'
        body += f'<p>默认末尾 {period["settings"]["validation_fraction"]:.0%}，至少3个月；本次实际尾段占比 {split["realized_validation_fraction"]:.1%}。{esc(split["reason"])}</p>'
    else:
        body += '<p>'+esc(split['reason'])+'</p>'
    body += '<p class="mut">'+esc(split['meaning'])+' 边界隔离用于避免后视窗口跨段；若标签或观察窗口长于默认隔离，须先提高隔离天数再复验。</p>'
    body += '<p class="mut">'+esc(period['regime']['meaning'])+'</p>'
    if period['non_default_settings']:
        labels = {'validation_fraction': '验证比例', 'minimum_triggers': '最少触发数', 'purge_days': '边界隔离日期数'}
        body += '<p class="notice">与默认值不同的设置：'+esc('；'.join(f'{labels[k]}={v}' for k,v in period['non_default_settings'].items()))+'</p>'
    from collections import Counter
    common = Counter(r['message'] for r in period['stages']).most_common(1)[0][0] if period['stages'] else ''
    special = [r for r in period['stages'] if r['message'] != common]
    body += f'<p>其余环节：{esc(common)}。</p>' if special else f'<p>全部环节：{esc(common)}。</p>'
    if special:
        body += '<div class="tw"><table><tr><th>环节</th><th>本次范围</th></tr>'
        for row in special:
            body += f'<tr><td>{esc(row["title"])}</td><td>{esc(row["message"])}</td></tr>'
        body += '</table></div>'
    return body+'</section>'


def summary_section(bundle, result, base):
    """Conclusions first: what a reader should take away before any detail."""
    from . import audit_report
    perf = audit_report.performance(bundle)
    cards = audit_actions.cards(result, bundle)
    body = '<section class="review-section" id="report-summary"><h2>结论摘要</h2><div class="report-answers">'
    for item in audit_report.answers(bundle, result, perf, result['modules'].get('premise'), cards):
        body += (f'<div class="report-answer"><h3>{esc(item["q"])} <span class="audit-verdict verdict-{item["tone"]}">'
                 f'{audit_report.STATE_WORDS[item["tone"]]}</span></h3><p><strong>{esc(item["lead"])}</strong></p>'
                 + ('<ul>' + ''.join(f'<li>{esc(n)}</li>' for n in item["notes"]) + '</ul>' if item["notes"] else '') + '</div>')
    body += '</div>'
    if perf:
        cells = [('区间收益', pct(perf['total'])), ('年化收益', pct(perf['annual']) if perf['annual'] is not None else '—'),
                 ('最大回撤', pct(perf['max_drawdown'])), ('夏普比率', f"{perf['sharpe']:.2f}" if perf['sharpe'] is not None else '—')]
        if perf.get('benchmark_total') is not None:
            cells += [('基准收益', pct(perf['benchmark_total'])), ('超额收益', f"{perf['excess_total']:+.2%}")]
        body += '<div class="review-metrics">' + ''.join(f'<div><span>{a}</span><strong>{b}</strong></div>' for a, b in cells) + '</div>'
    body += (f'<p class="review-resources"><a class="btn primary" href="{base}/report.html" target="_blank" rel="noopener">打开审计报告（可打印为 PDF）</a>'
             f'<a href="{base}/report.html?download=1">下载报告 HTML</a>'
             f'<a href="/strategy-attribution?strategy={base.split("/")[2]}#attribution-premise">查看策略前提检验</a></p></section>')
    return body


def partial_report(identity, bundle, result):
    review = result['input_review']; module = result['modules']['partial_diagnostics']
    body = heading('代码静态检查' if result['mode'] == 'static' else '材料受限的治理审计', f'/strategy-audits/{identity}', '核对材料')
    body += f'<p class="notice">材料核对与补件统一在<a href="/strategy-audits/{identity}">策略导入页</a>完成；本页按已确认材料展示结果。</p>'
    if result.get('period_review'):
        body += period_section(result['period_review'])
    if module['fills']:
        body += '<section class="review-section"><h2>可计算的费用与价位情景</h2><p class="notice">'+esc(module['meaning'])+'</p>'
        reported = '未知，不按0元处理' if module['reported_fees'] is None else '¥'+money(module['reported_fees'])
        body += f'<p>报告费用：{reported}；按确认费率的费用情景 ¥{money(module["modeled_fee_scenario"])}；价位半档情景 ¥{money(module["tick_cost_scenario"])}。</p>'
        body += '<p>不将这些情景计为优化收益；缺少完整账户和行情时，净盈亏及回撤不显示伪精确数字。</p></section>'
    if result['modules'].get('fee_analysis', {}).get('version', 1) >= 2:
        body += '<section class="review-section"><h2>按订单核对最低佣金</h2>'+fee_units_section(result['modules']['fee_analysis'], f'/strategy-audits/{identity}/reports/{{report_id}}')+'</section>'
    return body+f'<p><a href="/strategy-audits/{identity}/reports/{{report_id}}/evidence.json">下载证据 JSON</a></p>'


def action_cards(result, bundle=None, reruns=None):
    """Concrete changes first: finding, where to edit, saving vs sacrifice, evidence, what overturns it."""
    cards = audit_actions.attach_reruns(audit_actions.cards(result, bundle), reruns or {})
    if not cards:
        return ''
    body = '<section class="review-section" id="audit-actions"><h2>建议怎么改</h2><div class="action-cards">'
    for card in cards:
        body += f'<article class="action-card action-{card["state"]}" id="action-{card["id"]}">'
        body += f'<header><span class="action-state">{audit_actions.STATES[card["state"]]}</span><h3>{esc(card["title"])}</h3></header>'
        body += f'<p class="action-headline">{esc(card["headline"])}</p><p>{esc(card["finding"])}</p>'
        if card.get("table") and card.get("table_kind") == "timing":
            body += '<div class="tw"><table><tr><th>延后成交</th><th>笔数</th><th>盈亏变化</th><th>成交额覆盖</th></tr>'
            for row in card["table"]:
                body += f'<tr><td>{esc(row["label"])}</td><td>{row["fills"]}</td><td>¥{money(row["gain"])}</td><td>{pct(row["coverage"])}</td></tr>'
            body += '</table></div>'
        elif card.get("table"):
            body += '<div class="tw"><table><tr><th>持有期</th><th>笔数</th><th>净盈亏</th><th>扣价差情景后</th></tr>'
            for row in card["table"]:
                body += f'<tr><td>{esc(row["label"])}</td><td>{row["matches"]}</td><td>¥{money(row["net"])}</td><td>¥{money(row["net_after_tick"])}</td></tr>'
            body += '</table></div>'
        if card.get("edits"):
            body += '<div class="action-edits"><b>改代码哪里</b>'
            for edit in card["edits"]:
                where = (f'原策略第 {edit["line"]} 行' if edit.get("embedded") else f'第 {edit["line"]} 行') if edit["line"] else '回测设置'
                now = f'<code class="edit-now">{esc(edit["code"])}</code>' if edit["code"] else ''
                body += (f'<div class="action-edit"><span class="edit-line">{where}</span>{now}'
                         f'<code class="edit-new">→ {esc(edit["change"])}</code>'
                         f'<span class="mut">{esc(edit["note"])}</span></div>')
            body += '<p class="mut">只是指出位置，平台不改也不运行你的代码；改后回原平台重跑，再到『效果验证』上传对比。</p></div>'
        elif bundle is not None and card["id"] in {"holding_period", "low_price", "fee_setting", "minimum_commission"}:
            body += '<p class="mut action-edits">代码里没有找到控制这一项的设置，需要你在策略里自己定位。</p>'
        body += '<div class="action-options"><b>可选改法</b><ol>' + ''.join(f'<li><b>{esc(o["label"])}</b>：{esc(o["effect"])}</li>' for o in card["options"]) + '</ol></div>'
        body += '<dl class="action-ledger">'
        for label, key in (("省下", "saving"), ("可能放弃", "sacrifice"), ("净影响", "net")):
            body += f'<div><dt>{label}</dt><dd>{esc(card[key])}</dd></div>'
        body += '</dl>'
        if card.get("inputs"):
            body += '<dl class="action-ledger">' + ''.join(f'<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>' for k, v in card["inputs"]) + '</dl>'
            body += f'<p class="mut">计算：{esc(card["formula"])}</p>'
        for run in card.get("reruns", []):
            body += (f'<p class="action-rerun rerun-{esc(run["state"])}"><b>v{run["version"]} 同区间复跑：</b>{esc(run["verdict"])} '
                     f'<a href="/strategy-comparison?strategy={esc(run["parent"])}&against={esc(run["id"])}">查看对比</a></p>')
        for condition in card.get("conditions", []):
            body += f'<p class="notice">{esc(condition)}</p>'
        body += f'<p class="action-evidence"><span class="tag">{esc(audit_actions.EVIDENCE[card["evidence"]])}</span>{esc(card["basis"])}</p>'
        if card.get("rules"):
            body += '<p class="mut action-rules">依据：' + '；'.join(
                f'{esc(r["id"])} {esc(r["text"])}（{"经验阈值，未经样本检验" if r["kind"] == "threshold" else esc(r["source"])}）'
                for r in card["rules"]) + '</p>'
        body += f'<p class="mut">什么情况下这条建议不成立：{esc(card["overturn"])}</p></article>'
    return body + '</div></section>'


def _money_cell(value):
    cls = ' class="neg"' if value < 0 else ''
    return f'<td{cls}>¥{money(value)}</td>'


def premise_section(result):
    from . import audit_rules
    p = (result.get('modules') or {}).get('premise')
    head = '<section class="review-section" id="attribution-premise"><h2>你的策略前提：回测期间对了吗</h2>'
    if not p:
        return (head + '<p class="mut">没有识别出策略依据的因子，也没有导入每日打分，无法检验前提。上传每日打分（date, code, score，覆盖全部候选）'
                '和候选池收盘价（date, code, close）后，这里会回答：前提对不对、对多少、哪段时间对。</p></section>')
    tone = {'held': 'pass', 'reversed': 'adjust', 'not_supported': 'adjust'}.get(p['status'], 'evidence')
    body = head + f'<p>前提（{esc(p["source"])}）：<strong>{esc(p["premise"])}</strong></p>'
    code = p.get('code') or {}
    if code.get('factors') and not p.get('stated'):
        hits = '；'.join(f'第 {f["line"]} 行 <code>{esc(f["code"])}</code>（{esc(f["name"])}）' for f in code['factors'][:3])
        body += f'<p class="mut">识别依据：{hits}' + ('（来自代码内嵌的原策略）' if code.get('embedded') else '') + f'。{esc(code["note"])}。</p>'
    if p.get('conflict'):
        body += '<p class="notice">代码里的排序方向和策略实际选股方向不一致，检验按实际选股方向进行；请核对代码。</p>'
    body += f'<p class="audit-verdict verdict-{tone}">{esc(p["verdict"])}</p>'
    if p['status'] in ('needs_prices', 'needs_scores'):
        return body + '</section>'
    o = p['overall']
    # 1. 对不对 / 对多少
    lines = []
    if o['mean'] is not None:
        lines.append(f'对不对：{o["periods"]} 个调仓周期里，{o["positive_share"]:.0%} 的周期按前提排得靠前的标的整体涨得更多'
                     f'（秩相关 IC 平均 {o["mean"]:+.3f}' + (f'，t = {o["t"]:.2f}' if o['t'] is not None else '') + '）。'
                     if o['positive_share'] is not None else f'IC 平均 {o["mean"]:+.3f}。')
    bar = p['thresholds']['t']
    sig = lambda v: v is not None and abs(v) >= bar
    if p.get('spread_mean') is not None:
        t_txt = f'，t = {p["spread_t"]:.2f}' if p.get('spread_t') is not None else ''
        line = (f'对多少：按前提排在前三分之一的标的，每期平均比后三分之一{"多" if p["spread_mean"] >= 0 else "少"}涨 '
                f'{abs(p["spread_mean"]):.2%}（不计费用{t_txt}）')
        if sig(p.get('spread_t')) and p.get('spread_annual') is not None:
            line += f'，折合每年约 {p["spread_annual"]:+.1%}。'
        else:
            line += '。这个差距统计上不显著，不能当作前提带来的收益，也不宜折成年化。'
        lines.append(line)
    if p.get('selected_excess_mean') is not None:
        lines.append(f'实际选中的标的平均每期比候选池{"多" if p["selected_excess_mean"] >= 0 else "少"}涨 '
                     f'{abs(p["selected_excess_mean"]):.2%}，{p["selected_beat_share"]:.0%} 的周期跑赢候选池'
                     + ('。' if sig(p.get('selected_excess_t')) else '（统计上不显著）。'))
    if p.get('pool_compound') is not None and p.get('selected_compound') is not None:
        gap = p['selected_compound'] - p['pool_compound']
        if gap <= 0:
            tail = '：回测赚的钱主要来自候选池整体上涨，不是来自前提。'
        elif sig(p.get('selected_excess_t')):
            tail = f'：选股多出约 {gap:+.1%}。'
        else:
            tail = f'：选股多出约 {gap:+.1%}，但逐期差额不显著，这部分可能来自少数几期的运气。'
        lines.append(f'同期等权买入全部候选（不计费用）累计约 {p["pool_compound"]:+.1%}，选中的标的累计约 {p["selected_compound"]:+.1%}' + tail)
    body += '<ul class="attribution-lines">' + ''.join(f'<li>{esc(x)}</li>' for x in lines) + '</ul>'
    # 2. 哪段对
    words = {'held': '成立', 'reversed': '反了', 'unclear': '不显著', 'too_few': '期数太少'}
    groups = [('按年份', p.get('by_year') or []), ('按市场涨跌', p.get('by_market') or []),
              ('按开发段 / 验证段', [p['segments'][k] for k in ('development', 'validation') if k in (p.get('segments') or {})])]
    if any(rows for _, rows in groups):
        summary = []
        if p.get('held_years'):
            summary.append('前提成立的年份：' + '、'.join(p['held_years']))
        if p.get('reversed_years'):
            summary.append('前提反了的年份：' + '、'.join(p['reversed_years']))
        if not summary:
            summary.append('没有哪一年单独达到显著；逐年结果只看方向，期数少时波动很大')
        body += f'<h3>哪段时间对</h3><p>{esc("；".join(summary))}。</p>'
        body += '<div class="tw"><table><tr><th>时间段</th><th>周期数</th><th>前提占优的周期</th><th>IC 平均</th><th>t</th><th>前1/3减后1/3（每期）</th><th>判断</th></tr>'
        for title, rows in groups:
            if not rows:
                continue
            body += f'<tr><th colspan="7">{esc(title)}</th></tr>'
            for r in rows:
                body += (f'<tr><td>{esc(r["label"])}</td><td>{r["periods"]}</td>'
                         f'<td>{"" if r["positive_share"] is None else format(r["positive_share"], ".0%")}</td>'
                         f'<td>{"" if r["mean"] is None else format(r["mean"], "+.3f")}</td>'
                         f'<td>{"" if r["t"] is None else format(r["t"], ".2f")}</td>'
                         f'<td>{"" if r.get("spread_mean") is None else format(r["spread_mean"], "+.2%")}</td>'
                         f'<td>{words.get(r["status"], "")}</td></tr>')
        body += '</table></div>'
    th = p['thresholds']
    body += (f'<p class="action-evidence"><span class="tag">{esc(audit_rules.EVIDENCE["E2"])}</span>'
             f'{esc(p["scope"])}。收益区间：{esc(p["return_window"])}。判断口径：至少 {th["min_periods"]} 期；|t| ≥ {th["t"]:g} 才说成立或反了。'
             '分段越细期数越少，单段结果容易受运气影响。</p>')
    body += ('<p class="mut action-rules">依据：M-RANK-IC 每个调仓日全部候选的分数与随后收益做秩相关（因子检验常用方法）；'
             'T-PREMISE-T、T-PREMISE-MIN-PERIODS（经验阈值，未经样本检验）</p></section>')
    return body


def attribution_page(identity, report_id, result):
    data = audit_attribution.build(result)
    if not data:
        return None
    state = '账目已闭合' if data['verified'] else '账目待核对，以下为暂算'
    body = '<section class="review-section" id="attribution-summary"><h2>一句话看清盈亏</h2>'
    body += f'<p class="audit-verdict verdict-{"pass" if data["verified"] else "evidence"}">{state}</p>'
    body += '<ul class="attribution-lines">' + ''.join(f'<li>{esc(line)}</li>' for line in data['summary']) + '</ul></section>'

    body += premise_section(result)
    body += '<section class="review-section" id="attribution-waterfall"><h2>钱去哪了</h2><div class="waterfall">'
    peak = max(abs(w['value']) for w in data['waterfall']) or 1
    for w in data['waterfall']:
        width = max(2, round(abs(w['value']) / peak * 100))
        tone = 'total' if w.get('total') else ('neg' if w['value'] < 0 else 'pos')
        body += (f'<div class="waterfall-row"><span>{esc(w["label"])}</span>'
                 f'<span class="waterfall-bar"><i class="bar-{tone}" style="width:{width}%"></i></span>'
                 f'<strong class="{"neg" if w["value"] < 0 else ""}">¥{money(w["value"])}</strong></div>')
    body += '</div><p class="mut">价差为按最小价位 0.01 元估算的情景，不是实测成本；原回测若已计滑点，需先分离再比较。</p></section>'

    conc = data['concentration']
    body += '<section class="review-section" id="attribution-stocks"><h2>谁赚的、谁亏的</h2>'
    noun = conc.get('noun', '股票')
    body += (f'<p>{conc["stocks"]} 只{noun}：赚钱 {conc["winners"]} 只，合计 ¥{money(conc["winning_total"])}；'
             f'亏钱 {conc["losers"]} 只，合计 ¥{money(conc["losing_total"])}。</p><div class="attribution-split">')
    shown = min(5, len(conc['top']))
    for title, rows in ((f'赚得最多的 {shown} 只' if conc['bottom'] else f'全部 {conc["stocks"]} 只', conc['top']), ('亏得最多的 5 只', conc['bottom'])):
        if not rows:
            continue
        body += f'<div><h3>{title}</h3><div class="tw"><table><tr><th>{noun if noun != "标的" else "标的"}</th><th>费用前</th><th>费用</th><th>净贡献</th></tr>'
        for row in rows:
            body += f'<tr><td>{esc(row["code"])}</td>{_money_cell(row["gross_pnl"])}<td>¥{money(row["fees"])}</td>{_money_cell(row["net_pnl"])}</tr>'
        body += '</table></div></div>'
    body += '</div>'
    if conc['top5_net'] > 0 and data['net'] > 0 and conc['rest_net'] < 0:
        body += f'<p class="notice">结果集中：盈利前 5 只合计 ¥{money(conc["top5_net"])}，其余{noun}合计亏 ¥{money(-conc["rest_net"])}。少数股票决定了整体盈亏，换一段时间结果可能差别很大。</p>'
    body += '</section>'

    for key, title, label, note in (
            ('by_holding', '什么样的交易在赚、在亏：按持有期', '持有期', '按先进先出匹配买卖；只含已平仓。分组是事后描述，不证明改了持有期就会更好。'),
            ('by_price', '按买入价格区间', '买入价格', '低价股每个价位占价格的比例更高，买卖价差更贵；价差列为最小价位情景。')):
        rows = data[key]
        if not rows:
            continue
        body += f'<section class="review-section" id="attribution-{key}"><h2>{title}</h2><div class="tw"><table><tr><th>{label}</th><th>笔数</th><th>费用前</th><th>费用</th><th>净盈亏</th><th>扣价差情景后</th></tr>'
        for row in rows:
            body += (f'<tr><td>{esc(row["label"])}</td><td>{row["matches"]}</td>{_money_cell(row["gross"])}'
                     f'<td>¥{money(row["fees"])}</td>{_money_cell(row["net"])}{_money_cell(row["after_tick"])}</tr>')
        body += f'</table></div><p class="mut">{note}</p></section>'

    dd = data['drawdown']
    if dd:
        recovered = f'，{dd["recovered"]} 已回到前高' if dd['recovered'] else '，回测结束时尚未回到前高'
        body += f'<section class="review-section" id="attribution-drawdown"><h2>最大回撤亏在哪</h2><p>最大回撤 {pct(dd["ratio"])}：{dd["peak"]} 到 {dd["trough"]}，亏损 ¥{money(dd["loss"])}{recovered}。</p>'
        if dd['worst']:
            body += '<div class="tw"><table><tr><th>股票</th><th>回撤期间净贡献</th></tr>' + ''.join(
                f'<tr><td>{esc(r["code"])}</td>{_money_cell(r["net_pnl"])}</tr>' for r in dd['worst']) + '</table></div>'
        body += f'<p><a href="/strategy-audits/{identity}/reports/{report_id}#risk-diagnostics">查看回撤贡献与闭合核对</a></p></section>'
    body += f'<p class="mut">逐股贡献含已平仓与未平仓；它说明钱从哪来，不能区分能力和运气。<a href="/strategy-audits/{identity}/reports/{report_id}">完整报告与可复算明细</a></p>'
    return body


def _row(label, original, optimized=None):
    cells = f'<td>{esc(label)}</td>' + _money_cell(original)
    if optimized is not None:
        cells += _money_cell(optimized) + _money_cell(optimized - original)
    return '<tr>' + cells + '</tr>'


def card_choice(result):
    cards = audit_actions.cards(result)
    if not cards:
        return ''
    options = ''.join(f'<option value="{esc(c["id"])}">{esc(c["title"])}</option>' for c in cards)
    return ('<label>这次重跑验证的是哪张卡<select name="tests_card"><option value="">不指定（只比较两版）</option>'
            f'{options}</select></label>')


def verification_page(identity, record, result, versions, compare_with, token):
    restated = audit_verification.restate(result)
    if not restated:
        return '<section class="review-section"><h2>效果验证</h2><p class="notice">需要完整的交易与持仓材料，才能按费率情景重算。</p></section>'
    t = audit_verification.totals(restated)
    rate = result.get('settings', {})
    confirmed = rate.get('broker_confirmed')
    rate_label = f"{rate.get('commission_bp', 0):g}bp（{'已确认合同费率' if confirmed else '未确认，仅为情景'}）"
    body = '<section class="review-section" id="restated"><h2>第一步：按费率情景重算原回测</h2>'
    body += (f'<p>不重跑策略，只把原回测的每一笔成交重算：佣金按 {rate_label}、印花税按成交日期适用税率，'
             '再加最小价位的买卖价差情景。</p>')
    if not confirmed:
        body += '<p class="notice">券商合同费率未确认：下面是费率情景，不代表实盘成本。在审计设置里确认合同费率后重新审计。</p>'
    body += '<div class="tw"><table><tr><th></th><th>金额</th></tr>'
    body += _row('原回测净盈亏', t['reported'])
    body += _row('费用复算差（原回测多扣为正）', t['fee_gap'])
    body += _row('买卖价差情景', -t['spread'])
    body += _row('按费率情景重算后', t['realistic'])
    body += '</table></div>'
    dd = restated['drawdown']
    body += f'<p>最大回撤：原回测 {pct(-dd["reported"])}，重算后 {pct(-dd["realistic"])}。</p>'
    if restated['unpriced_fills']:
        body += f'<p class="notice">{restated["unpriced_fills"]} 笔成交无法复算费用，按原回测费用计入。</p>'
    tone = 'neg' if t['realistic'] < 0 else ''
    body += f'<p class="restated-verdict {tone}">原回测显示 ¥{money(t["reported"])}，按 {rate_label}重算约为 ¥{money(t["realistic"])}。</p>'
    body += '<p class="mut">价差是情景估计；原回测若已计滑点或价差，需先分离，避免重复扣除。</p></section>'

    body += '<section class="review-section" id="compare"><h2>第二步：用优化版回测验证改法</h2>'
    others = [v for v in versions if v['id'] != identity]
    if compare_with:
        other_record, other_result = compare_with
        cmp = audit_verification.compare(result, other_result)
        if not cmp:
            body += '<p class="notice">优化版缺少完整的交易与持仓材料，无法用同一方法重算。</p>'
        elif not cmp['comparable']:
            body += f'<p class="notice">{esc(cmp["reason"])}</p>'
        else:
            body += f'<p class="compare-verdict compare-{cmp["state"]}">{esc(cmp["verdict"])}</p>'
            body += f'<p class="mut">比较区间 {cmp["start"]} 至 {cmp["end"]}；两版都按同一方法重算成本。' + ('' if cmp['same_period'] else '两次回测区间不同，只比较重叠部分。') + '</p>'
            for key, title in (('full', '全区间'), ('validation', '验证段（审计没有用这段数据给建议）')):
                if key not in cmp['rows']:
                    continue
                a, b = cmp['rows'][key]
                body += f'<h3>{title}</h3><div class="tw"><table><tr><th></th><th>{esc(display_name(record))}</th><th>{esc(display_name(other_record))}</th><th>差额</th></tr>'
                body += _row('原平台报告净盈亏', a['reported'], b['reported'])
                body += _row('买卖价差情景', -a['spread'], -b['spread'])
                body += _row('按真实成本重算后', a['realistic'], b['realistic'])
                body += f'<tr><td>成交笔数</td><td>{a["fills"]}</td><td>{b["fills"]}</td><td>{b["fills"] - a["fills"]:+d}</td></tr>'
                body += '</table></div>'
            body += f'<p>重算后最大回撤：原版 {pct(-cmp["drawdown"][0])}，优化版 {pct(-cmp["drawdown"][1])}。</p>'
            body += '<p class="mut">这是同一段历史上的对比：结果更好不等于未来一定更好。验证段没有参与审计建议，比全区间更有参考价值；最终以实盘或纸面跟踪为准。</p>'
    if others:
        body += f'<form method="get" action="/strategy-comparison" class="review-form compare-picker"><input type="hidden" name="strategy" value="{identity}"><label>选择要对比的版本<select name="against">'
        for v in others:
            sel = ' selected' if compare_with and compare_with[0]['id'] == v['id'] else ''
            body += f'<option value="{v["id"]}"{sel}>{esc(display_name(v))}</option>'
        body += '</select></label><button class="secondary">对比</button></form>'
    body += ('<ol class="verify-steps"><li>在“策略治理”页生成优化代码候选，下载 .py。</li>'
             '<li>回原平台用同一回测区间、同一初始资金重新回测。</li>'
             '<li>导出交易、持仓、日志，在下方上传为新版本；系统按同一方法重算并对比。</li></ol>')
    body += (f'<form method="post" action="/strategy-audits/{identity}/optimized" enctype="multipart/form-data" class="review-form">'
             f'<input type="hidden" name="token" value="{token}">'
             + card_choice(result) +
             '<label>上传优化版回测导出（交易、持仓、日志、优化后代码；ZIP 不用解压）<input type="file" name="files" accept=".csv,.txt,.py,.zip,.json,.xlsx" multiple></label>'
             '<button class="primary">上传优化版并核对</button></form>')
    body += '<p class="mut">上传后进入导入核对页，确认后成为新版本；原版本与报告保持不变。</p></section>'
    return body


def coverage_section(bundle, result, *, identity='', expand_reviews=True):
    verdicts = {r['id']: r for r in audit_verdicts.conclusions(bundle, result)}
    cards = result.get('coverage')
    if not cards:
        cards = coverage(bundle, result) if 'accounting' in result['modules'] else [
            {**s, 'metrics': [], 'observation': verdicts[s['id']]['reason'], 'algorithms': [],
             'candidate_action': '补齐本环节所需材料后重新审计。', 'tradeoff': '不据缺失材料推断策略有效或无效。'} for s in STAGES]
    body = '<section class="review-section" id="coverage"><h2>逐环节审计</h2><p class="mut">基本通过仅指本次已检查范围；缺乏证据不等于策略失败。展开查看依据与调整建议。</p><div class="audit-coverage">'
    for index, item in enumerate(cards, 1):
        metrics = ''
        for m in item["metrics"]:
            value = '—' if m['value'] is None else pct(m['value']) if m['unit'] == 'ratio' else ('¥'+money(m['value']) if m['unit'] == 'CNY' else f'{m["value"]:g}')
            metrics += f'<div><span>{esc(m["label"])}</span><strong>{value}</strong></div>'
        verdict = verdicts[item['id']]
        body += f'<details class="audit-stage" id="stage-{item["id"]}"'+(' open' if expand_reviews and verdict['state'] == 'adjust' else '')+'>'
        body += f'<summary><span class="audit-stage-number">{index:02d}</span><span>{esc(item["title"])}<small>{esc(verdict["summary"])}</small></span><span class="audit-verdict verdict-{verdict["state"]}">{verdict["label"]}</span></summary>'
        body += f'<div class="audit-stage-body"><p class="verdict-reason verdict-{verdict["state"]}">{esc(verdict["reason"])}</p><p>{esc(item["observation"])}</p>'
        period = next((r for r in result.get('period_review', {}).get('stages', []) if r['id'] == item['id']), None)
        if period and period['state'] != 'length_eligible':
            body += '<p class="notice audit-period-gate">'+esc(period['message'])+'</p>'
        if metrics:
            body += '<div class="audit-stage-metrics">'+metrics+'</div>'
        if item.get('algorithms'):
            body += '<details><summary>本次检查范围</summary><div class="tw"><table class="audit-readiness"><tr><th>检查内容</th><th>本次范围</th></tr>'
            for method in item['algorithms']:
                missing = '<br><small>'+esc('；'.join(method['missing']))+'</small>' if method['missing'] else ''
                state = '已计算' if method['implementation'] == 'connected' else '本次未检验'
                if method['implementation'] == 'connected' and method['inputs'] == 'needs_confirmation':
                    state = '已读取，待核对'
                elif method['implementation'] == 'connected' and method['inputs'] == 'needs_reconciliation':
                    state = '已暂算，待对账'
                elif method['implementation'] == 'connected' and method['inputs'] == 'missing':
                    state = '缺少匹配材料'
                body += f'<tr><td>{esc(method["name"])}</td><td>{state}{missing}</td></tr>'
            body += '</table></div></details>'
        body += f'<p><b>建议调整：</b>{esc(item["candidate_action"])}</p><p><b>代价与边界：</b>{esc(item["tradeoff"])}</p><p class="mut"><b>补充材料：</b>{esc("；".join(item["needs"]))}</p><p class="mut">方法依据：{esc(item["basis"])}</p>'
        if identity:
            stage_kinds = {
                'signal_contract': ('scores',), 'signal_age': ('scores',), 'portfolio_mapping': ('scores',),
                'universe': ('scores',), 'execution': ('logs',), 'account_integrity': ('positions', 'logs'),
                'attribution': ('positions',), 'risk_exposure': ('positions',), 'costs': ('fills',),
            }.get(item['id'], ())
            available = {s['kind'] for s in bundle['sources']}
            missing_kinds = [k for k in stage_kinds if k not in available]
            if missing_kinds:
                body += '<div class="material-actions">'+''.join(material_links(identity, k) for k in missing_kinds)+'</div>'
        body += '</div></details>'
    return body+'</div></section>'


def code_section(identity, report_id, bundle, result, token):
    inspection = audit_code.inspect(bundle, result)
    body = '<section class="review-section" id="optimize-code"><h2>按审计发现修改代码</h2><p>保留原因子、打分和选股，选择本次治理修改，生成完整代码与改动对照。</p>'
    if not inspection['options']:
        body += '<p class="notice">当前未找到可安全自动改写的治理位置。不会只加说明注释就称作优化代码。</p><p class="mut">'+esc('；'.join(inspection['unavailable']))+'</p></section>'
        return body
    body += f'<form method="post" action="/strategy-audits/{identity}/reports/{report_id}/code-candidates" class="review-form"><input type="hidden" name="token" value="{token}"><input type="hidden" name="result_digest" value="{result["result_digest"]}">'
    for option in inspection['options']:
        body += f'<label class="review-check"><input type="checkbox" name="changes" value="{option["id"]}" checked>{esc(option["title"])}</label><p>{esc(option["reason"])}</p>'
        if option['id'] == 'small_buy_guard':
            body += f'<label>最低新增买入金额（元）<input name="minimum_buy_notional" type="number" min="0.01" max="1000000000" step="0.01" value="{math.ceil(option["default_threshold"]*100)/100:.2f}"></label><p class="mut">按已确认最低佣金÷佣金率预填金额拐点，可调整；不是收益最优值，也不是强制放大订单。</p>'
        body += '<p class="mut">'+esc(option['tradeoff'])+'</p>'
    body += '<button class="primary">生成优化代码候选</button></form><p class="notice">本平台不运行用户代码。下载后需回原平台重新回测；未证明收益改善，也不自动启用策略。</p></section>'
    return body


def code_candidate_body(identity, report_id, candidate_id, candidate):
    base = f'/strategy-audits/{identity}/reports/{report_id}/code-candidates/{candidate_id}'
    body = heading('优化代码候选', '/strategy-governance?strategy='+identity, '返回策略治理')
    body += '<p class="notice">待原平台回测 · '+esc(candidate['meaning'])+'</p><section class="review-section"><h2>这版改了什么</h2>'
    for change in candidate['changes']:
        body += '<article class="audit-finding"><h3>'+esc(change['title'])+'</h3><p>'+esc(change['reason'])+'</p><p class="mut">'+esc(change['tradeoff'])+'</p></article>'
    body += f'<div class="material-actions"><a class="btn primary" href="{base}/optimized.py">下载优化代码 .py</a><a href="{base}/changes.patch">下载改动对照 .patch</a></div></section>'
    body += '<section class="review-section"><h2>改动对照</h2><pre class="code-diff">'
    for line in candidate['diff'].splitlines():
        color = 'diff-add' if line.startswith('+') else 'diff-remove' if line.startswith('-') else ''
        body += f'<span class="{color}">{esc(line)}</span>\n'
    body += '</pre><details><summary>原代码与完整候选代码</summary><div class="code-comparison"><div><h3>原代码</h3><pre>'+esc(candidate['source'])+'</pre></div><div><h3>候选代码 · 待回测</h3><pre>'+esc(candidate['optimized_source'])+'</pre></div></div></details></section>'
    return body


def research_section(result, base):
    tick, holding = (result['modules'].get(k) for k in ('tick_analysis', 'holding_analysis'))
    if not tick or not holding:
        return ''
    interval = tick['accounted_intervals']; all_fills = tick['all_exported_fills']
    body = '<section class="review-section" id="tick-cost"><h2>价位成本：收益能否覆盖半档价差情景</h2>'
    body += '<p>方法来源：STRUCT 的一档报价地板分量。0.01元为一档，按每边半档估计，半价差 = 50 / 成交价（bp）。</p>'
    bp = all_fills['weighted_half_spread_bp']
    body += f'<p>全部导出成交额 ¥{money(all_fills["traded_notional"])}，加权半档代理 <b>{bp:.2f} bp / 每边</b>。</p>' if bp is not None else '<p>本次没有可计算成交。</p>'
    if interval['fills']:
        verdict = ('本情景下，原净盈亏足以覆盖该成本。' if interval['profit_covers_scenario']
                   else '本情景下，原净盈亏不足以覆盖该成本。' if interval['profit_covers_scenario'] is False
                   else '账目尚未闭合，覆盖结论暂不确认。')
        body += f'<div class="review-metrics"><div><span>已归因区间原净盈亏</span><strong>¥{money(interval["net_pnl"])}</strong></div><div><span>该区间半档情景成本</span><strong>¥{money(interval["tick_scenario_cost"])}</strong></div><div><span>再扣该情景后的净盈亏</span><strong>¥{money(interval["net_after_tick_scenario"])}</strong></div></div><p class="notice">{verdict}固定原成交计算，尚未改写资金和后续交易。</p>'
        if interval['break_even_cost_fraction'] is not None:
            fraction = interval['break_even_cost_fraction']
            ratio = pct(fraction) if fraction <= 1 else f'{fraction:.2f}倍'
            body += f'<p>保持原盈亏不变时，可覆盖的成本为当前半档情景的 {ratio}。这不是降低相同比例换手就能保住收益的保证。</p>'
    body += f'<details><summary>方法与成交机制边界</summary><p>{esc(tick["meaning"])}</p><p class="mut">{esc(tick["overlap"])}</p>'
    body += f'<p>报告时间落在09:30或15:00的成交有 {tick["auction_boundary_fills"]} 笔；仅凭时间不能确认成交机制，竞价成本需另行核验。</p>'
    body += '</details>'
    body += '<h3>低价暴露：摩擦主要花在哪里</h3><div class="tw"><table><tr><th>成交价分组</th><th>成交额占比</th><th>成交额</th><th>报告费用</th><th>半档情景成本</th></tr>'
    for row in tick['price_bands']:
        if not row['traded_notional']:
            continue
        share = pct(row['notional_share']) if row['notional_share'] is not None else '—'
        body += f'<tr><td>{esc(row["label"])}</td><td>{share}</td><td>{money(row["traded_notional"])}</td><td>{money(row["reported_fees"])}</td><td>{money(row["tick_scenario_cost"])}</td></tr>'
    body += f'</table></div><p><b>候选调整：</b>{esc(tick["candidate_action"])}</p>'
    calibration = tick['calibration']
    body += '<h3>与L2报价核对</h3>'
    if calibration['matched_fills']:
        body += f'<p>匹配 {calibration["matched_fills"]} 笔，覆盖成交额 {pct(calibration["notional_coverage"])}。匹配样本的半档代理 {calibration["weighted_tick_half_spread_bp"]:.2f}bp，日均报价半价差 {calibration["weighted_quoted_half_spread_bp"]:.2f}bp。</p><p>有 {calibration["below_tick_proxy_fills"]} 笔对应的日均报价低于成交价代理的98%，需检查日内价格变化、竞价/交叉报价和来源清洗。未按代理下限截断。</p>'
        body += f'<p class="mut">{esc(calibration["meaning"])}</p>'
    else:
        body += '<p>尚无同证券同日期的有效报价匹配。可补充日均报价表核对量级；成交时点与竞价成本仍需完整盘口和执行记录。</p>'
    body += f'<a href="{base}/tick-costs.csv">下载逐笔价位情景 CSV</a></section>'
    body += '<section class="review-section" id="struct-reference"><h2>成本口径对照</h2><div class="tw"><table><tr><th>主口径：价位半档情景</th><th>参考：完整STRUCT滞后估计</th></tr>'
    body += '<tr><td>已按成交价计算，非实盘成本下限；集合竞价另核。</td><td>当前未提供对应上月的日均成交额、波动率与不复权价格，未计算完整估计。校准 struct_lagged_2021_2023_v1；作者SSRN模型，未经同行评审。</td></tr></table></div></section>'
    body += '<section class="review-section" id="holding-age"><h2>实际持有期与收益覆盖</h2>'
    body += '<p>从实际成交做 FIFO 批次归因，检验不同持有期的毛收益能否覆盖报告费用和半档情景。代码参数 hold_days 不自动当成强制退出期限。</p>'
    coverage = holding['coverage']; closed = holding['closed_summary']; opened = holding['open_summary']
    body += f'<p>可匹配已平仓片段 {closed["matches"]} 个、{closed["quantity"]:g} 股；未平仓片段 {opened["matches"]} 个、{opened["quantity"]:g} 股（右删失，单列）。一次卖出可能匹配多个买入片段，片段数不等于交易笔数。</p>'
    if holding['quantity_weighted_calendar_days'] is not None:
        body += f'<p>已平仓按股数加权持有 {holding["quantity_weighted_calendar_days"]:.2f} 个自然日，跨 {holding["quantity_weighted_snapshot_intervals"]:.2f} 个导出快照间隔；不冒充完整交易日历。</p>'
    body += '<div class="tw"><table><tr><th>实际持有期</th><th>匹配股数</th><th>毛收益</th><th>报告费用</th><th>净收益</th><th>双边半档情景</th><th>情景后净收益</th></tr>'
    for row in holding['closed_age_bands']:
        body += f'<tr><td>{esc(row["label"])}</td><td>{row["quantity"]:g}</td><td>{money(row["gross_pnl"])}</td><td>{money(row["fees"])}</td><td>{money(row["net_pnl"])}</td><td>{money(row["tick_scenario_cost"])}</td><td>{money(row["net_after_tick_scenario"])}</td></tr>'
    body += '</table></div><h3>按买入价看贡献，未平仓另列</h3><div class="tw"><table><tr><th>买入价分组</th><th>已平仓毛收益</th><th>已平仓费用</th><th>已平仓净收益</th><th>已平仓半档情景</th><th>未平仓净贡献</th></tr>'
    for row in holding['entry_price_bands']:
        c, o = row['closed'], row['open']
        if not c.get('matches') and not o.get('matches') and not o['net_pnl']:
            continue
        body += f'<tr><td>{esc(row["label"])}</td><td>{money(c["gross_pnl"])}</td><td>{money(c["fees"])}</td><td>{money(c["net_pnl"])}</td><td>{money(c["tick_scenario_cost"])}</td><td>{money(o["net_pnl"])}</td></tr>'
    body += '</table></div><p class="mut">买入价分组与上面的逐笔成交价分组口径不同。未平仓净贡献为期末市值减分摊买入金额及买入费；买入半档仅在情景中扣除，未虚构卖出和退出费用。</p>'
    body += f'<p>覆盖限制：{coverage["excluded_action_or_invalid_codes"]} 只有公司行动或数量/估值异常的证券整段暂不匹配，共 {coverage["excluded_fill_count"]} 笔成交、金额 ¥{money(coverage["excluded_notional"])}。未知期初被卖出 {coverage["unknown_opening_sale_quantity"]:g} 股；未匹配异常 {len(coverage["unmatched"])} 项。</p>'
    if coverage['net_bridge_comparable']:
        body += f'<p>覆盖证券的“已平仓 + 未平仓”净贡献与逐股账本差额 ¥{money(coverage["net_bridge_gap"])}。未覆盖证券仍完整保留在原逐股归因；这里的分组不能代表全策略。</p>'
    body += f'<details><summary>批次分配方法与覆盖限制</summary><p class="mut">{esc(holding["meaning"])}</p><p class="mut">{esc(holding["coverage_note"])}</p></details><p><b>下一步复验：</b>{esc(holding["policy_note"])}</p>'
    if holding['status'] != 'verified':
        body += '<p class="notice warn">账户尚未闭合，本节均为暂算，不据此确认可改善的持有期。</p>'
    body += f'<a href="{base}/holding-lots.csv">下载全部持仓批次归因 CSV</a></section>'
    return body


def fee_units_section(cost, base):
    body = f'<p>{cost["priced_fill_count"]} 笔可定价成交，共 {len(cost["charge_units"])} 个收费单位；<b>{cost["confirmed_unit_count"]} 个已有计费依据且可核验</b>，{cost["unknown_basis_unit_count"]} 个缺依据或完整性，{cost["unpriced_unit_count"]} 个日期未覆盖。已确认单位中 {cost["mismatch_count"]} 个差额超过 ¥0.02，已确认差额合计 ¥{money(cost["confirmed_fee_gap"])}。</p>'
    body += f'<p>识别到 {cost["partial_order_count"]} 个多次成交的委托。按委托归并后，比逐成交收最低佣金的情景少计 ¥{money(cost["avoided_duplicate_minimum_scenario"])}；这是<b>修正记账口径，不是交易节省</b>。</p>'
    if cost['unknown_basis_unit_count']:
        body += '<p class="notice">下一步：在策略导入页补委托编号与合同/交割单计费依据；按委托计费须确认该委托最终全部成交已导出。当前情景差额不判为多收费。</p>'
        identity = base.split('/reports/')[0].rsplit('/', 1)[-1]
        body += '<div class="material-actions">'+material_links(identity, 'fills')+'</div>'
    body += '<details><summary>收费单位明细</summary><div class="tw"><table><tr><th>日期 / 委托</th><th>证券 / 成交数</th><th>口径</th><th>金额</th><th>报告费用</th><th>模型费用</th><th>差额</th></tr>'
    for unit in cost['charge_units'][:50]:
        label = ('按委托' if unit['basis'] == 'order' else '按成交')+(' · 已有依据' if unit['status'] == 'confirmed' else ' · 情景')
        body += f'<tr><td>{esc(unit["date"])}<br>{esc(unit["order_id"] or "未提供委托号")}</td><td>{esc(unit["code"])} / {unit["fill_count"]}</td><td>{label}</td><td>{money(unit["notional"])}</td><td>{money(unit["reported_fee"])}</td><td>{money(unit["modeled_fee"]) if unit["modeled_fee"] is not None else "未覆盖日期"}</td><td>{money(unit["reported_minus_modeled"]) if unit["reported_minus_modeled"] is not None else "—"}</td></tr>'
    body += f'</table></div><p>页面最多展示50个单位，完整记录含计费依据见 <a href="{base}/fee-units.csv">下载收费单位核验 CSV</a>。</p></details><p class="mut">{esc(cost["gap_interpretation"])}</p>'
    return body


def diagnostics_section(result, base):
    modules = result['modules']
    cost, risk, behavior = (modules.get(k) for k in ('fee_analysis', 'risk_analysis', 'trading_behavior'))
    if not cost or not risk or not behavior:
        return '<p class="notice">这是历史版本报告，未运行新增费用与风险检查。可返回材料页重新分析，生成新报告。</p>'
    body = '<section class="review-section" id="computed-actions"><h2>费用核验与可计算建议</h2>'
    body += '<h3>01 · 核对费用口径</h3>'
    if cost.get('version', 1) >= 2:
        body += fee_units_section(cost, base)
    else:
        body += f'<p>{cost["priced_fill_count"]} 笔可定价成交中，{cost["mismatch_count"]} 笔费用差额超过 ¥0.02。报告费用减模型费用合计 <b>¥{money(cost["reported_minus_modeled"])}</b>，逐笔差额绝对值合计 ¥{money(cost["absolute_gap"])}。</p>'
    body += f'<p>模型佣金 ¥{money(cost["commission"])}，历史印花税 ¥{money(cost["stamp_tax"])}；未覆盖日期的成交 {cost["unpriced_fill_count"]} 笔。</p><p class="mut">{esc(cost["gap_interpretation"])}</p>'
    body += '<details><summary>02 · 同刻同向合单检查（有匹配时再展开）</summary>'
    if cost['batch_candidates']:
        body += f'<p>{len(cost["batch_candidates"])} 组候选在确认收费口径和决策批次后，模型佣金最多少 <b>¥{money(cost["conditional_batch_saving"])}</b>。价格不利变动预算如下，超过预算就会吃掉节费。</p>'
        body += '<div class="tw"><table><tr><th>日期 / 时刻</th><th>证券 / 方向</th><th>笔数</th><th>总金额</th><th>条件节费</th><th>价格预算 bp</th></tr>'
        for row in cost['batch_candidates'][:25]:
            side = '买入' if row['side'] == 'buy' else '卖出'
            body += f'<tr><td>{row["date"]}<br>{esc(row["time"])}</td><td>{esc(row["code"])} / {side}</td><td>{row["fill_count"]}</td><td>{money(row["notional"])}</td><td>{money(row["conditional_saving"])}</td><td>{row["adverse_price_budget_bp"]:.2f}</td></tr>'
        body += '</table></div>'
    else:
        body += '<p>本次未发现可量化最低佣金节省的同刻同向合单组，保留原成交；不据此建议跨时段等待或买卖轧差。</p>'
    body += f'<p class="mut">{esc(cost["batch_conditions"])} 未识别有效时刻的成交 {cost["unknown_clock_fills"]} 笔，未参与合单检查。</p>'
    body += '</details>'
    if cost['breakpoint_notional'] is not None:
        body += f'<p>确认费率下，单笔金额到 <b>¥{money(cost["breakpoint_notional"])}</b> 后，比例佣金达到最低佣金。这是费用拐点，不是最低下单金额或必须放大仓位的建议。</p>'
    stress = cost['cost_stress']
    body += '<h3>03 · 净盈亏还能容纳多少额外成本</h3>'
    if stress['positive_profit_exhausted_at_extra_bp'] is not None:
        body += f'<p>在已归因区间，每侧再增加 <b>{stress["positive_profit_exhausted_at_extra_bp"]:.2f} bp</b> 的成本就会耗尽当前正净盈亏；当前无正净盈亏时预算为 0。</p>'
    else:
        body += '<p>没有可计算的归因区间。</p>'
    body += '<div class="tw"><table><tr><th>每侧额外成本</th><th>新增费用</th><th>情景净盈亏</th></tr>'
    for row in stress['scenarios']:
        body += f'<tr><td>{row["extra_bp_per_side"]:g} bp</td><td>¥{money(row["extra_cost"])}</td><td>¥{money(row["net_after_extra_cost"])}</td></tr>'
    body += f'</table></div><p class="mut">{esc(stress["meaning"])}</p>'
    if stress['account_status'] != 'verified':
        body += '<p class="notice warn">账目尚未闭合，成本预算与归因数值均为暂算，不据此确认改善。</p>'
    body += f'<a href="{base}/fees.csv">下载逐笔费用核验 CSV</a></section>'
    body += '<section class="review-section" id="risk-diagnostics"><h2>风险、约束与回撤来自哪里</h2>'
    dd = risk['max_drawdown']
    state = '已核对' if risk['status'] == 'verified' else '暂算，账目待核对'
    body += f'<p>最大快照回撤 <b>{pct(dd["ratio"])}</b>，峰值到谷底权益减少 ¥{money(dd["loss"])}（{state}）。</p>'
    if dd['trough_date']:
        peak = dd['peak_date'] or '已声明空仓期初'
        recovery = dd['recovery_date'] or '导出范围内未恢复峰值'
        body += f'<p>{esc(peak)} → {dd["trough_date"]}，{dd["intervals_to_trough"]} 个快照间隔；恢复：{esc(recovery)}。</p>'
        body += f'<p>这段回撤已扣费用 ¥{money(dd["fees"])}，逐股净贡献合计与权益变化差 ¥{money(dd["contribution_gap"])}。</p><div class="tw"><table><tr><th>证券</th><th>回撤期费用前贡献</th><th>费用</th><th>净贡献</th></tr>'
        for row in dd['stocks'][:20]:
            body += f'<tr><td>{esc(row["code"])}</td><td>{money(row["gross_pnl"])}</td><td>{money(row["fees"])}</td><td>{money(row["net_pnl"])}</td></tr>'
        body += '</table></div><p class="mut">按净贡献从小到大展示前 20 项，全部明细在证据 JSON。</p>'
    cap = risk['cap_repair']
    body += f'<h3>期末单票上限 {pct(cap["max_weight"])}：需要调整多少</h3>'
    if cap['positions']:
        body += f'<p>{cap["date"]} 有 {len(cap["positions"])} 只超限。费用前至少减持市值 <b>¥{money(cap["marked_sale_lower_bound"])}</b>，才可能压回已确认上限。</p>'
        body += '<div class="tw"><table><tr><th>证券</th><th>当前权重</th><th>必要减持下界</th></tr>'
        for row in cap['positions']:
            body += f'<tr><td>{esc(row["code"])}</td><td>{pct(row["current_weight"])}</td><td>¥{money(row["marked_sale_lower_bound"])}</td></tr>'
        body += '</table></div>'
    else:
        body += '<p>期末未触及已确认的单票上限；无需为这一项额外交易。</p>'
    body += f'<p class="mut">{esc(cap["meaning"])}</p><p class="mut">{esc(risk["meaning"])}</p>'
    body += f'<p>同证券同日双向交易 {len(behavior["same_day_two_way"])} 组，重叠金额 ¥{money(behavior["two_way_notional"])}。</p><p class="mut">{esc(behavior["meaning"])}</p>'
    body += f'<a href="{base}/risk.csv">下载快照风险统计 CSV</a></section>'
    return body


def report_body(identity, report_id, bundle, result):
    if result.get('mode', 'full') != 'full':
        return partial_report(identity, bundle, result).replace('{report_id}', report_id)
    account = result["modules"]["accounting"]; attr = result["modules"]["attribution"]
    gov = result["modules"]["governance"]
    base = f"/strategy-audits/{identity}/reports/{report_id}"
    body = heading(bundle["name"], f"/strategy-audits/{identity}", "核对材料与重新分析")
    body += f'<p class="review-meta">{bundle["snapshots"][0]["date"]} 至 {bundle["snapshots"][-1]["date"]} · {len(account["daily"])} 个归因期间</p>'
    state = "账目已核对" if attr["status"] == "verified" else "账目待核对，归因暂为草稿"
    body += f'<p class="notice">{state} · '+('从空仓期初开始' if account['baseline'] == 'declared_empty_opening' else '从首个收盘账户之后开始')+'</p>'
    body += summary_section(bundle, result, base)
    if result.get('period_review'):
        body += period_section(result['period_review'])
    body += '<nav class="audit-tabs" aria-label="报告章节"><a href="#tick-cost">价位成本</a><a href="#holding-age">持有期</a><a href="#computed-actions">费用与建议</a><a href="#risk-diagnostics">风险与回撤</a><a href="#coverage">全流程覆盖</a><a href="#contributions">盈亏归因</a><a href="#governance-audit">治理与候选动作</a><a href="#reconciliation">账户核对</a></nav>'
    body += '<div class="review-metrics">'
    for label, value in (("区间净盈亏", '¥'+money(attr['net_pnl'])), ("费用前贡献", '¥'+money(attr['gross_pnl'])), ("已扣交易费用", '¥'+money(attr['fees'])), ("前三名 / 正贡献", pct(attr['top3_positive_share']) if attr['top3_positive_share'] is not None else '—')):
        body += f'<div><span>{label}</span><strong>{value}</strong></div>'
    body += '</div>'+research_section(result, base)+diagnostics_section(result, base)+coverage_section(bundle, result)+'<section class="review-section" id="contributions"><h2>盈亏来自哪些标的</h2>'
    body += f'<p>盈利贡献合计 ¥{money(attr["positive_total"])}，亏损贡献合计 ¥{money(attr["negative_total"])}。</p>'
    if attr['top1_positive_share'] is not None:
        body += f'<p>最大单只{"标的" if any(asset_class(r["code"]) != "stock" for r in attr["stocks"]) else "股票"}占全部正贡献的 {pct(attr["top1_positive_share"])}。该比例反映盈利集中度，不等同于策略能力。</p>'
    body += '<div class="tw"><table class="holdings-table"><tr><th>证券</th><th>费用前盈亏</th><th>费用</th><th>净贡献</th><th>基线资金 bp</th></tr>'
    for row in attr["stocks"]:
        bp = f'{row["initial_capital_bp"]:.2f}' if row['initial_capital_bp'] is not None else '—'
        body += f'<tr><td>{esc(row["code"])}</td><td>{money(row["gross_pnl"])}</td><td>{money(row["fees"])}</td><td>{money(row["net_pnl"])}</td><td>{bp}</td></tr>'
    body += '</table></div><p class="mut">'+esc(attr['interpretation'])+'</p>'
    if attr["external_report_status"] == "unverified_external":
        body += '<p class="notice">平台行业归因原表已保留在证据包，尚缺日期、单位与计算口径，未合并进已核对的逐股贡献。</p>'
    body += f'<a href="{base}/stocks.csv">下载全部逐股贡献 CSV</a></section>'
    body += '<section class="review-section" id="governance-audit"><h2>治理诊断与候选动作</h2>'
    labels = {"review": "待处理", "consistent": "与当前口径一致", "opportunity": "可量化摩擦", "measured": "已测量", "needs_input": "待补充"}
    for item in gov["items"]:
        observed = '—' if item['observed'] is None else (pct(item['observed']) if item['key'] == 'turnover' else f'{item["observed"]:,.2f}')
        body += f'<div class="audit-finding"><h3>{esc(item["title"])} <span class="tag">{labels[item["status"]]}</span></h3><p><strong>{observed}</strong> {esc(item["unit"])}</p><p>{esc(item["action"])}</p></div>'
    body += f'<p class="notice">小单费用情景按确认的佣金 {gov["settings"]["commission_bp"]:g}bp、最低佣金 ¥{gov["settings"]["minimum_commission"]:g} 计算，覆盖 {gov["small_fill_count"]} 个小额收费单位情景。它是原成交的费用敏感性，不代表可实现节省。当前未完成历史方案重放，未输出“最优收益”结论。</p>'
    body += f'<a class="btn primary" href="/strategy-audits/{identity}/plan?report_id={report_id}">带入期末持仓，准备账户方案</a></section>'
    body += '<section class="review-section" id="reconciliation"><h2>账户核对</h2><div class="tw"><table><tr><th>核对项</th><th>检查数</th><th>异常数</th><th>最大差额</th></tr>'
    for row in account['summary']:
        body += f'<tr><td>{CHECK_NAMES[row["key"]]}</td><td>{row["count"]}</td><td>{row["failures"]}</td><td>{row["max_gap"]:.4f}</td></tr>'
    body += '</table></div>'
    if account['exceptions']:
        body += '<details><summary>异常明细</summary><div class="tw"><table><tr><th>日期</th><th>证券</th><th>项目</th><th>差额</th></tr>'
        for row in account['exceptions'][:200]:
            body += f'<tr><td>{row["date"]}</td><td>{esc(row["code"])}</td><td>{CHECK_NAMES[row["key"]]}</td><td>{row["gap"]:.4f}</td></tr>'
        body += '</table></div><p>完整异常明细见证据 JSON。</p></details>'
    body += '</section>'
    body += f'<div class="review-resources"><a href="{base}/evidence.json">下载证据 JSON</a><a href="{base}/daily.csv">下载每日贡献 CSV</a></div>'
    return body


def register(app, workdir, *, page, guard, token):
    def load(identity):
        try:
            return store.read(workdir, identity)
        except (ValueError, OSError, KeyError, TypeError):
            raise HTTPException(404, "导入不存在或完整性核对失败") from None

    def load_report(identity, report_id):
        try:
            return store.report(workdir, identity, report_id)
        except (ValueError, OSError, KeyError, TypeError):
            raise HTTPException(404, "报告不存在或完整性核对失败") from None

    @app.get('/strategy-audits')
    def index():
        body = heading('我的策略', '/strategy-governance', '返回策略治理')
        body += '<div class="review-submit"><a class="btn primary" href="/strategy-audits/new">策略导入</a><a href="/strategy-audits/sample.zip">下载合成示例</a></div><section class="review-section"><h2>已导入策略</h2><div class="review-history">'
        records = store.history(workdir, latest_only=True)
        for record in records:
            report_id, _ = current_report(record['id'])
            state = '导入已确认' if report_id else '材料待确认'
            identity = record['id']
            body += f'<article class="strategy-list-item" data-strategy="{identity}"><div class="strategy-list-heading"><strong>{esc(display_name(record))}</strong><span class="tag">{state}</span><a href="/strategy-audits/{identity}">材料管理</a></div><div class="strategy-quick-actions">'
            if report_id:
                body += f'<form method="post" action="/strategy-audits/{identity}/rerun"><input type="hidden" name="token" value="{token}"><button class="primary">一键审计</button></form>'
            else:
                body += f'<a class="btn primary" href="/strategy-audits/{identity}">一键审计</a>'
            for route, label in (('strategy-attribution', '一键归因'), ('strategy-comparison', '效果验证')):
                body += f'<a class="btn" href="/{route}?strategy={identity}">{label}</a>'
            body += '</div><p class="mut">'+('审计复用已确认参数；回测需有可比较版本，模拟需更新当前账户和信号。' if report_id else '先完成导入核对，四项功能共用确认后的材料。')+'</p></article>'
        body += '</div>'+('' if records else '<p class="mut">暂无导入记录</p>')+'</section>'
        return page('我的策略', body, tab='import')

    @app.get('/strategy-audits/new')
    def new():
        return page('策略导入', upload_form(token), tab='import')

    def template_download(kind, adapter):
        try:
            filename, text = materials.template(kind, adapter)
        except ValueError:
            raise HTTPException(404, '材料模板不存在') from None
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(filename, ('\ufeff'+text).encode('utf-8'))
            hint = materials.MATERIALS[kind][1]
            instructions = f'{materials.MATERIALS[kind][0]}填写说明\n\n{hint}\n日期用YYYY-MM-DD，股票代码如600000.SH / 000001.SZ。\n模板只有字段，不含示例数据；请填入自己的真实回测材料。\n必须与其他文件属于同一次回测；同类补传将整份替换，原版本和报告保留。\n'
            if kind == 'positions' and adapter == 'bigquant':
                instructions += '每个日期保留平台汇总行：证券代码留空，持仓均价列为“总资产: 金额”，收盘价列为“现金: 金额”，持仓市值列为当日持仓总市值。推荐直接上传原导出。\n'
            if kind == 'positions' and adapter == 'joinquant':
                instructions += '股票数量可带“股”，标的可用证券名(600000.XSHG)；Cash行在标的列填Cash、市值/价值列填现金。保留总资产列。\n'
            if kind == 'fills':
                instructions += 'side使用buy/sell；BigQuant买/卖列使用买入/卖出。fee/交易费用为整笔成交费用（元）。\n'
            archive.writestr('填写说明.txt', ('\ufeff'+instructions).encode('utf-8'))
        return Response(stream.getvalue(), media_type='application/zip',
                        headers={'Content-Disposition': f'attachment; filename="{kind}-template.zip"'})

    @app.get('/strategy-audits/templates/{kind}.zip')
    def new_template(kind: str, adapter: str = 'generic'):
        return template_download(kind, adapter)

    @app.get('/strategy-audits/{identity}/templates/{kind}.zip')
    def existing_template(identity: str, kind: str):
        _, bundle = load(identity)
        return template_download(kind, bundle['adapter'].removesuffix('_v1') if bundle['adapter'] != 'code_only_v1' else 'generic')

    @app.get('/strategy-audits/rules')
    def rules_page():
        from . import audit_rules
        body = heading('审计规则登记表', '/strategy-audits', '我的策略')
        body += '<section class="review-section"><p>每条审计结论都标注它用到的规则编号。规则事实不可调；经验阈值是项目自己的判断，可在导入确认页的“判断口径”里调整。</p>'
        for kind in ('fact', 'method', 'threshold'):
            body += f'<h2>{audit_rules.KIND_NAMES[kind]}</h2><div class="tw"><table><tr><th>编号</th><th>内容</th><th>默认值</th><th>来源</th></tr>'
            for r in audit_rules.RULES.values():
                if r.kind == kind:
                    value = audit_rules._fmt(r, r.value) if r.value is not None else ''
                    body += f'<tr><td>{esc(r.id)}</td><td>{esc(r.text)}</td><td>{esc(value)}</td><td>{esc(r.source)}</td></tr>'
            body += '</table></div>'
        return page('审计规则', body + '</section>')

    @app.get('/strategy-audits/sample.zip')
    def sample():
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            for source in sample_files():
                archive.writestr(source.name, source.data)
        return Response(stream.getvalue(), media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="synthetic-strategy.zip"'})

    @app.post('/strategy-audits/import')
    async def create(req: Request):
        # Check Host before reading uploads; authenticate the token before any persistent writes.
        guard(req, token)

        form = await upload_fields(req)
        try:
            guard(req, form.get('token'))
            files = await uploaded_files(form)
            cash = form.get('opening_cash', '')
            if cash and form.get('opening_empty') != 'yes':
                raise ValueError('填写期初现金时，请先确认空仓期初和期间无外部出入金')
            if form.get('opening_empty') == 'yes' and not cash:
                raise ValueError('确认空仓期初后，请填写期初现金')
            bundle = await run_in_threadpool(imports.normalize, files, adapter=str(form.get('adapter', 'auto')),
                name=str(form.get('name', '')), opening_cash=cash or None)
            style = str(form.get('style', '')).strip()
            if len(style) > 40:
                raise ValueError('风格标签不能超过40个字符')
            bundle['metadata'] = {'style': {'value': style, 'source': 'user_confirmed' if style else 'unconfirmed'}}
            identity = await run_in_threadpool(store.save, workdir, bundle, files)
        except (ValueError, TypeError, OSError) as exc:
            response = page('导入失败', upload_form(token, str(exc)), tab='import')
            response.status_code = 400
            return response
        finally:
            await form.close()
        return RedirectResponse('/strategy-audits/' + identity, 303)

    @app.post('/strategy-audits/{identity}/materials')
    async def supplement(identity: str, req: Request):
        guard(req, token)
        _, previous = load(identity)
        form = await upload_fields(req)
        try:
            guard(req, form.get('token'))
            if form.get('input_digest') != ex.object_digest(previous):
                raise ValueError('材料摘要不一致，请重新打开导入页')
            additions = await uploaded_files(form)
            originals = await run_in_threadpool(store.source_files, workdir, identity)
            files, bundle = await run_in_threadpool(materials.replace_files, originals, previous, additions, str(form.get('kind', '')))
            updated = await run_in_threadpool(store.save, workdir, bundle, files, parent=identity)
        except (ValueError, TypeError, OSError) as exc:
            body = '<p role="alert" class="notice warn">'+esc(str(exc))+'</p>'+overview(identity, previous, token)
            response = page('补充材料未保存', body, identity=identity, tab='import')
            response.status_code = 400
            return response
        finally:
            await form.close()
        return RedirectResponse('/strategy-audits/' + updated, 303)

    @app.post('/strategy-audits/{identity}/optimized')
    async def optimized_version(identity: str, req: Request):
        guard(req, token)
        _, previous = load(identity)
        form = await upload_fields(req)
        try:
            guard(req, form.get('token'))
            additions = await uploaded_files(form)
            originals = await run_in_threadpool(store.source_files, workdir, identity)
            files, bundle = await run_in_threadpool(materials.replace_version, originals, previous, additions)
            card = str(form.get('tests_card') or '').strip()
            if card:
                if len(card) > 40 or not card.replace('_', '').isalnum():
                    raise ValueError('卡片编号无效')
                bundle['metadata'] = {**bundle.get('metadata', {}), 'tests_card': card}
            updated = await run_in_threadpool(store.save, workdir, bundle, files, parent=identity)
        except (ValueError, TypeError, OSError) as exc:
            response = page('优化版未保存', '<p role="alert" class="notice warn">'+esc(str(exc))+'</p><p><a href="/strategy-comparison?strategy='+identity+'">返回效果验证</a></p>', identity=identity, tab='comparison')
            response.status_code = 400
            return response
        finally:
            await form.close()
        return RedirectResponse('/strategy-audits/' + updated, 303)

    @app.get('/strategy-audits/{identity}')
    def show(identity: str):
        record, bundle = load(identity)
        inherited = None
        if record.get('parent_id'):
            parent_report = current_report(record['parent_id'])[1]
            inherited = (parent_report or {}).get('settings')
        body = overview(identity, bundle, token, inherited)
        if record.get('parent_id'):
            body += f'<p class="mut">材料版本 v{record["version"]}，尚需再次确认。<a href="/strategy-audits/{record["parent_id"]}">查看上一个材料版本</a></p>'
        paths = sorted((store.root(workdir, identity) / 'reports').glob('*/result.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        if paths:
            body += '<section class="review-section"><h2>已有报告</h2>'
            for index, path in enumerate(paths):
                body += f'<p><a href="/strategy-audits/{identity}/reports/{path.parent.name}">报告 {len(paths)-index}</a></p>'
            body += '</section>'
        return page('策略导入', body, identity=identity, tab='import')

    @app.post('/strategy-audits/{identity}/analyze')
    async def analyze(identity: str, req: Request):
        form = await req.form(); guard(req, form.get('token'))
        _, bundle = load(identity)
        try:
            if form.get('input_digest') != ex.object_digest(bundle):
                raise ValueError('材料版本已变化，请重新核对')
            if form.get('same_run') != 'yes':
                raise ValueError('请确认材料来自同一次回测并核对参数')
            settings = {key: float(form[key]) for key in ('commission_bp', 'minimum_commission', 'max_weight')}
            settings['max_weight'] /= 100
            settings['broker_confirmed'] = form.get('broker_confirmed') == 'yes'
            settings['thresholds'] = threshold_values(form)
            text = str(form.get('premise_text', '')).strip()[:200]
            direction = str(form.get('premise_direction', '')).strip()
            if direction not in ('', 'higher', 'lower'):
                raise ValueError('分数方向无效')
            if text or direction:
                settings['premise'] = {'text': text, 'direction': direction}
            claimed = {}
            for key in ('total', 'annual'):
                raw = str(form.get('claimed_' + key, '')).strip().rstrip('%')
                if raw:
                    claimed[key] = float(raw) / 100
            if claimed:
                settings['claim'] = {**claimed, 'source': str(form.get('claim_source', '')).strip()[:200]}
            policy = {'validation_fraction': float(form.get('validation_fraction', 25)) / 100,
                      'minimum_triggers': float(form.get('minimum_triggers', 20)),
                      'purge_days': float(form.get('purge_days', 20))}
            result = await run_in_threadpool(audit.analyze, bundle, settings, policy=policy)
            result['confirmation'] = {'same_run': True, 'settings': settings,
                                      'scope': 'report_assumptions_only_not_live_rules'}
            for candidate in result['input_review']['parameters']:
                if candidate['key'] in settings and candidate['value'] == settings[candidate['key']]:
                    candidate['confirmed'] = True
            result['result_digest'] = ex.object_digest({k:v for k,v in result.items() if k != 'result_digest'})
            report_id = await run_in_threadpool(store.save_report, workdir, identity, result)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None
        target = f'/strategy-governance?strategy={identity}' if form.get('destination') == 'workspace' else f'/strategy-audits/{identity}/reports/{report_id}'
        record, current = load(identity)
        parent = record.get('parent_id')
        # A re-run backtest goes straight to the comparison with the version it came from.
        if parent and current.get('metadata', {}).get('replaced_kinds') and form.get('destination') == 'workspace':
            target = f'/strategy-comparison?strategy={parent}&against={identity}'
        return RedirectResponse(target, 303)

    @app.post('/strategy-audits/{identity}/rerun')
    async def rerun(identity: str, req: Request):
        guard(req, token)
        form = await req.form()
        guard(req, form.get('token'))
        _, bundle = load(identity)
        _, previous = current_report(identity)
        if not previous or not previous.get('confirmation', {}).get('same_run'):
            return RedirectResponse('/strategy-audits/'+identity, 303)
        # Same settings and the same judgement thresholds as the confirmed report.
        settings = {**previous['settings'], 'thresholds': (previous.get('rules') or {}).get('thresholds')}
        settings.pop('premise', None)
        if previous['settings'].get('premise'):
            settings['premise'] = previous['settings']['premise']
        result = await run_in_threadpool(audit.analyze, bundle, settings,
            policy=previous.get('period_review', {}).get('settings'))
        result['confirmation'] = previous['confirmation']
        for candidate in result['input_review']['parameters']:
            if candidate['key'] in result['settings'] and candidate['value'] == result['settings'][candidate['key']]:
                candidate['confirmed'] = True
        result['result_digest'] = ex.object_digest({k: v for k, v in result.items() if k != 'result_digest'})
        await run_in_threadpool(store.save_report, workdir, identity, result)
        return RedirectResponse('/strategy-governance?strategy='+identity, 303)

    @app.get('/strategy-audits/{identity}/reports/{report_id}')
    def report(identity: str, report_id: str):
        _, bundle, result = load_report(identity, report_id)
        return page('归因与治理审计', report_body(identity, report_id, bundle, result), identity=identity)

    @app.get('/strategy-audits/{identity}/reports/{report_id}/report.html')
    def report_document(identity: str, report_id: str, download: int = 0):
        from . import audit_report
        record, bundle, result = load_report(identity, report_id)
        if result.get('mode', 'full') != 'full':
            raise HTTPException(404, '材料不全的审计没有正式报告')
        headers = {'Content-Disposition': 'attachment; filename="strategy-audit-report.html"'} if download else {}
        return Response('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>'
                        + audit_report.render(record, bundle, result) + '</body></html>',
                        media_type='text/html; charset=utf-8', headers=headers)

    @app.get('/strategy-audits/{identity}/reports/{report_id}/evidence.json')
    def evidence(identity: str, report_id: str):
        record, bundle, result = load_report(identity, report_id)
        return JSONResponse({'record': record, 'input': bundle, 'result': result}, headers={'Content-Disposition': 'attachment; filename="strategy-audit-evidence.json"'})

    @app.post('/strategy-audits/{identity}/reports/{report_id}/code-candidates')
    async def create_code_candidate(identity: str, report_id: str, req: Request):
        guard(req, token)
        form = await req.form(); guard(req, form.get('token'))
        _, bundle, result = load_report(identity, report_id)
        try:
            if form.get('result_digest') != result['result_digest']:
                raise ValueError('审计结果已变化，请重新查看报告')
            candidate = await run_in_threadpool(audit_code.propose, bundle, result, form.getlist('changes'),
                minimum_buy_notional=form.get('minimum_buy_notional') or None)
            candidate_id = await run_in_threadpool(store.save_code_candidate, workdir, identity, report_id, candidate)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse(f'/strategy-audits/{identity}/reports/{report_id}/code-candidates/{candidate_id}', 303)

    def load_candidate(identity, report_id, candidate_id):
        try:
            return store.code_candidate(workdir, identity, report_id, candidate_id)
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(404, '代码候选不存在或摘要不一致') from None

    @app.get('/strategy-audits/{identity}/reports/{report_id}/code-candidates/{candidate_id}')
    def show_code_candidate(identity: str, report_id: str, candidate_id: str):
        candidate = load_candidate(identity, report_id, candidate_id)
        body = code_candidate_body(identity, report_id, candidate_id, candidate)
        body += strategy_picker(workdir, identity, 'governance')
        return page('优化代码候选', body, identity=identity)

    @app.get('/strategy-audits/{identity}/reports/{report_id}/code-candidates/{candidate_id}/{kind}')
    def export_code_candidate(identity: str, report_id: str, candidate_id: str, kind: str):
        candidate = load_candidate(identity, report_id, candidate_id)
        if kind == 'optimized.py':
            content = candidate['optimized_source'].encode(candidate['encoding'])
            filename = 'optimized_candidate.py'
        elif kind == 'changes.patch':
            content = candidate['diff'].encode('utf-8')
            filename = 'governance_changes.patch'
        else:
            raise HTTPException(404)
        return Response(content, media_type='application/octet-stream',
                        headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    @app.get('/strategy-audits/{identity}/reports/{report_id}/{kind}.csv')
    def export(identity: str, report_id: str, kind: str):
        from . import audit_exports
        _, _, result = load_report(identity, report_id)
        found = audit_exports.table(result, kind)
        if found is None:
            if kind == 'fee-units' and 'fee_analysis' in result['modules']:
                raise HTTPException(404, '历史报告尚无收费单位核验，请重新审计')
            raise HTTPException(404, '本报告尚无该项结果')
        rows, columns = found
        return Response('﻿'+csv_text(rows, columns), media_type='text/csv', headers={'Content-Disposition': f'attachment; filename="audit-{kind}.csv"'})

    @app.get('/strategy-audits/{identity}/plan')
    def plan(identity: str, report_id: str | None = None):
        _, bundle = load(identity)
        if not bundle['snapshots'] or any(p.get('price') is None for p in bundle['positions']):
            raise HTTPException(400, '需补充账户现金和持仓价格后才能带入账户方案')
        confirmed_rules = {}
        if report_id:
            _, _, result = load_report(identity, report_id)
            confirmed_rules['max_weight'] = result['settings']['max_weight']
        last = bundle['snapshots'][-1]
        positions = [p for p in bundle['positions'] if p['date'] == last['date']]
        if len(positions) > 100:
            raise HTTPException(400, '当前账户规划最多支持 100 只证券，请核对后手动导入')
        value = {'name': (bundle['name']+' · 账户方案')[:80], 'observed_at': '',
            'signal_available_at': '', 'signal_version': 'pending-current-signal',
            'execution_date': '', 'signal_kind': 'target_weight', 'cash': last['cash'], 'rules': confirmed_rules,
            'holdings': [{'code': p['code'], 'quantity': p['quantity'], 'sellable_quantity': 0} for p in positions],
            'quotes': [{'code': p['code'], 'price': p['price'], 'status': 'conditional'} for p in positions]}
        body = '<p class="notice warn">已带入历史期末持仓和现金，尚未视为当前账户。请更新当前持仓、可卖数量、价格、参考时点与交易日，补充最新预测后再计算；保存方案不会写入成交或账户。</p>'
        body += editor(token, form_values(value))
        return page('准备账户方案', body, identity=identity, tab='simulation')

    def selected(strategy):
        if strategy:
            return strategy
        records = store.history(workdir, latest_only=True)
        return records[0]['id'] if records else ''

    def current_report(identity):
        paths = sorted((store.root(workdir, identity) / 'reports').glob('*/result.json'),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not paths:
            return None, None
        report_id = paths[0].parent.name
        _, _, result = load_report(identity, report_id)
        return report_id, result

    def rerun_evidence(identity, record, result):
        """{card id: comparison} for confirmed child versions that declared which card they test."""
        out = {}
        family = record.get('strategy_id', record['id'])
        for child in store.history(workdir):
            if child.get('parent_id') != identity or child.get('strategy_id', child['id']) != family:
                continue
            try:
                _, child_bundle = load(child['id'])
            except HTTPException:
                continue
            card = (child_bundle.get('metadata') or {}).get('tests_card')
            other = current_report(child['id'])[1] if card else None
            if not other:
                continue
            cmp = audit_verification.compare(result, other)
            if cmp and cmp.get('comparable'):
                out.setdefault(card, []).append({**cmp, 'version': child.get('version'), 'id': child['id'], 'parent': identity})
        return out

    def workspace(tab, strategy, against=''):
        if strategy == 'new':
            return RedirectResponse('/strategy-audits/new', 303)
        identity = selected(strategy)
        if not identity:
            body = heading('策略治理', '/strategy-audits', '全部策略')
            body += strategy_picker(workdir, tab=tab) + workflow()
            body += '<section class="review-section audit-empty"><h2>尚未导入策略</h2><div class="review-submit"><a class="btn primary" href="/strategy-audits/new">导入策略与回测</a><a href="/strategy-audits/sample.zip">下载合成示例</a></div></section>'
            body += '<section class="review-section"><h2>账户调整</h2><div class="review-resources"><a href="/review/new">新建账户诊断</a><a href="/review/new?example=1">载入合成示例</a><a href="/review">已有诊断记录</a></div></section>'
            return page('策略治理', body, tab=tab)
        record, bundle = load(identity)
        report_id, result = current_report(identity)
        title = '账户方案' if tab == 'simulation' else dict((k, label) for k, _, label in TABS)[tab]
        body = heading(title, f'/strategy-audits/{identity}', '材料管理 / 补充上传')
        body += strategy_picker(workdir, identity, tab)
        if bundle['snapshots']:
            body += '<p class="review-meta">回测材料区间 '+esc(bundle['snapshots'][0]['date'])+' 至 '+esc(bundle['snapshots'][-1]['date'])+'</p>'
        body += workflow(identity, tab if result else 'import')
        if not result:
            body += f'<section class="review-section"><h2>导入尚未确认</h2><p>请先在导入页完成材料核对、补充与参数确认。完成后，四个页面共用这份策略。</p><a class="btn primary" href="/strategy-audits/{identity}">继续完成策略导入</a></section>'
            return page(title, body, identity=identity, tab=tab)
        if tab == 'governance':
            verdicts = audit_verdicts.conclusions(bundle, result)
            body += workspace_summary(result)
            body += '<nav class="audit-tabs" aria-label="治理章节"><a href="#audit-findings">审计发现</a>'
            if audit_actions.cards(result):
                body += '<a href="#audit-actions">建议怎么改</a>'
            body += '<a href="#optimize-code">代码候选</a><a href="#coverage">环节与证据</a></nav>'
            body += '<section class="review-section" id="audit-findings"><h2>本次审计发现</h2><div class="audit-verdict-counts">'
            for state, label in audit_verdicts.LABELS.items():
                count = sum(r['state'] == state for r in verdicts)
                body += f'<span class="audit-verdict verdict-{state}">{label} {count} 项</span>'
            body += '</div>'
            issues = [r for r in verdicts if r['state'] == 'adjust']
            if issues:
                for row in issues[:3]:
                    body += f'<p class="verdict-reason verdict-adjust"><a href="#stage-{row["id"]}">{esc(row["title"])}</a>：{esc(row["reason"])}</p>'
            else:
                body += '<p>已完成检查中未确认需要调整的问题；未覆盖环节仍需补充证据。</p>'
            if result.get('mode', 'full') != 'full':
                module = result['modules']['partial_diagnostics']
                body += '<p class="notice">'+('仅代码静态检查，未验证交易效果。' if result['mode'] == 'static' else esc(module['meaning']))+'</p>'
                if module['fills']:
                    fee = '未知，不按0元处理' if module['reported_fees'] is None else '¥'+money(module['reported_fees'])
                    body += '<p>报告费用：'+fee+'</p>'
            body += f'<p><a href="/strategy-audits/{identity}/reports/{report_id}">查看完整报告与可复算明细</a> · <a href="/strategy-attribution?strategy={identity}">查看盈亏归因</a></p></section>'
            body += action_cards(result, bundle, rerun_evidence(identity, record, result))
            body += '<section class="review-section audit-next"><div><h2>下一步</h2><p class="mut">代码调整需回原平台回测；账户方案需补充当前信号与可卖数量。</p></div><div class="review-resources">'
            body += f'<a href="/strategy-comparison?strategy={identity}">导入新回测，验证改动</a><a href="/strategy-simulation?strategy={identity}">准备账户调整方案</a></div></section>'
            body += code_section(identity, report_id, bundle, result, token)
            candidates = sorted((store.root(workdir, identity) / 'reports' / report_id / 'code_candidates').glob('*/candidate.json'), key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                body += '<section class="review-section"><h2>已生成的代码候选</h2>'
                for path in candidates:
                    body += f'<p><a href="/strategy-audits/{identity}/reports/{report_id}/code-candidates/{path.parent.name}">查看代码候选与改动对照</a> · 待原平台回测</p>'
                body += '</section>'
            body += coverage_section(bundle, result, identity=identity, expand_reviews=False)
            missing = [c for c in result['input_review']['checks'] if c['status'] in {'missing', 'blocked', 'approximate'}]
            if missing:
                body += '<details class="import-details"><summary>补充材料可扩展的诊断范围</summary><p class="mut">补件统一返回导入页；上传后重新核对和确认，不需要重传已有文件。补齐材料不表示算法自动接入。</p>'
                kinds = dict.fromkeys(k for c in missing for k in materials.CHECK_MATERIALS.get(c['key'], ()))
                body += '<div class="material-actions">'+''.join(material_links(identity, k) for k in kinds if not (k == 'snapshots' and bundle['adapter'] in {'bigquant_v1', 'joinquant_v1'}))+'</div></details>'
        elif tab == 'attribution':
            rendered = attribution_page(identity, report_id, result) if result else None
            if rendered:
                body += rendered
            elif result and result.get('mode', 'full') == 'full':
                attr = result['modules']['attribution']; risk = result['modules'].get('risk_analysis')
                state = '账目已核对' if attr['status'] == 'verified' else '账目待核对，以下为暂算'
                body += f'<section class="review-section"><h2>盈亏归因</h2><p class="notice">{state}；净贡献 ¥{money(attr["net_pnl"])} = 费用前贡献 ¥{money(attr["gross_pnl"])} − 已扣费用 ¥{money(attr["fees"])}。</p>'
                body += '<div class="tw"><table><tr><th>证券</th><th>费用前贡献</th><th>费用</th><th>净贡献</th></tr>'
                for row in attr['stocks']:
                    body += f'<tr><td>{esc(row["code"])}</td><td>{money(row["gross_pnl"])}</td><td>{money(row["fees"])}</td><td>{money(row["net_pnl"])}</td></tr>'
                body += '</table></div><p class="mut">'+esc(attr['interpretation'])+'</p></section>'
                if risk:
                    body += f'<p>最大快照回撤 {pct(risk["max_drawdown"]["ratio"])}。<a href="/strategy-audits/{identity}/reports/{report_id}#risk-diagnostics">查看回撤贡献与闭合核对</a></p>'
            else:
                kinds = {src['kind'] for src in bundle['sources']}
                if 'fills' in kinds and 'positions' not in kinds:
                    why, need = '只有交易记录，没有每日持仓和现金，算不出每天的账户盈亏。', 'positions'
                elif bundle.get('quality', {}).get('fills_inferred'):
                    why, need = '交易是从持仓变化推算的，费用未知，不显示精确盈亏。', 'fills'
                else:
                    why, need = '交易、持仓与账户还没有对上，先在材料管理里核对。', 'positions'
                body += (f'<p class="notice">当前没有可闭合的归因报告：{why}补上后即可归因。</p>'
                         f'<div class="material-actions">{material_links(identity, need)}</div>')
        elif tab == 'comparison':
            versions = [r for r in store.history(workdir) if r.get('strategy_id', r['id']) == record.get('strategy_id', record['id'])]
            other = None
            if against and against != identity and any(v['id'] == against for v in versions):
                other_report = current_report(against)[1]
                if other_report:
                    other = (load(against)[0], other_report)
            body += verification_page(identity, record, result, versions, other, token)
            body += '<details class="import-details"><summary>查看已确认的验证分段与可审范围</summary>'+period_section(result['period_review'] if result.get('period_review') else audit_period.assess(bundle))+'</details>'
            body += '<section class="review-section" hidden><h2>原版与优化版</h2><p>尚未导入可对比的新版本。参数调整需保持原信号与可交易条件一致；修改选股后，请回原平台重新回测。</p><p>仅有原成交不能恢复未入选股票或模拟另一套调仓规则，本页尚未给出优化效果。</p></section>'
        else:
            body += '<section class="review-section"><h2>每日执行准备</h2><p>当前策略材料已共用，无需重复上传。历史期末持仓需要更新为当前账户，并补充当天目标持仓或信号。</p>'
            if bundle['snapshots']:
                body += f'<a class="btn primary" href="/strategy-audits/{identity}/plan">带入期末持仓，准备账户方案</a>'
            else:
                body += '<p>请先在导入页补充持仓、现金与价格。</p><div class="material-actions">'+material_links(identity, 'positions')+'</div>'
            body += '<p class="mut">确认方案只保存计划，不开通账户、不下单、不当作成交。</p></section>'
        return page(title, body, identity=identity, tab=tab)

    @app.get('/strategy-governance')
    def governance_workspace(strategy: str = ''):
        return workspace('governance', strategy)

    @app.get('/strategy-attribution')
    def attribution_workspace(strategy: str = ''):
        return workspace('attribution', strategy)

    @app.get('/strategy-comparison')
    def comparison_workspace(strategy: str = '', against: str = ''):
        return workspace('comparison', strategy, against)

    @app.get('/strategy-simulation')
    def simulation_workspace(strategy: str = ''):
        return workspace('simulation', strategy)

    @app.get('/settings')
    def settings():
        body = heading('设置与其他工具', '/strategy-governance', '返回策略治理')
        body += '<section class="review-section"><h2>数据与已有工具</h2><div class="review-resources"><a href="/data">行情数据</a><a href="/review">账户治理方案</a><a href="/paper">原纸面账户</a><a href="/holdings">模拟持仓</a><a href="/governance">治理实验</a><a href="/audit">原外部预测检验</a><a href="/start">原策略流程</a><a href="/train">原模型训练</a><a href="/models">原模型记录</a></div><p class="mut">已有地址和记录继续可用，不自动迁移或启用账户。</p></section>'
        return page('设置', body)
