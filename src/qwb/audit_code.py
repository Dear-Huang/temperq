"""Conservative AST-located governance patches. Uploaded code is never executed."""
from __future__ import annotations

import ast
from difflib import unified_diff
from hashlib import sha256
import math
import re

from .experiments import object_digest


def _tree(source):
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def inspect(bundle, result):
    source = bundle.get('code', {}).get('source', '')
    tree = _tree(source)
    options, unavailable = [], []
    if tree is None or not source.strip():
        return {'options': [], 'unavailable': ['代码语法尚未解析，不能定位安全改写位置。']}
    fees = result.get('modules', {}).get('fee_analysis')
    settings = result['settings']
    # Never mistake a small partial fill of a large order for a small order.
    fee_units = fees.get('charge_units', bundle.get('fills', [])) if fees else []
    small_buys = [f for f in fee_units if f['side'] == 'buy'
                  and f['notional'] * settings['commission_bp'] / 10000 < settings['minimum_commission']]
    candidates = []
    for node in ast.walk(tree):
        # Only this exact positive-cash branch is known to be incremental buys.
        if not isinstance(node, ast.If) or node.orelse or len(node.body) != 1:
            continue
        test = node.test
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Gt)
                and isinstance(test.left, ast.Name) and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant) and type(test.comparators[0].value) in (int, float)
                and test.comparators[0].value == 0):
            continue
        statement = node.body[0]
        call = statement.value if isinstance(statement, ast.Expr) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name) and call.func.value.id == 'context'
                and call.func.attr == 'order_value' and len(call.args) == 2 and not call.keywords
                and isinstance(call.args[1], ast.Name) and call.args[1].id == test.left.id
                and statement.lineno == statement.end_lineno):
            continue
        candidates.append(statement)
    if bundle['adapter'] == 'bigquant_v1' and fees and small_buys and settings['commission_bp'] > 0 and candidates:
        options.append({'id': 'small_buy_guard', 'finding': 'costs.minimum_commission',
            'title': '减少最低佣金约束下的小额买入',
            'reason': f'{len(small_buys)}个买入收费单位情景未达到比例佣金覆盖最低佣金的金额；安全定位到{len(candidates)}处正金额买入。订单归属或合同未确认时，先补收费依据再决定是否采用。',
            'default_threshold': settings['minimum_commission'] / settings['commission_bp'] * 10000,
            'lines': [s.lineno for s in candidates],
            'tradeoff': '跳过小额买入会保留更多现金、增加目标偏离，甚至漏掉盈利机会；不拦截卖出。金额拐点不是收益最优阈值。'})
    else:
        unavailable.append('当前未同时满足真实小单记录、已确认费率和可安全识别的BigQuant正金额买入分支，不自动套用买入过滤。')
    caps = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == 'context' and target.attr == 'max_cash_per_instrument'):
            caps.append(node)
    cap = settings['max_weight']
    if (bundle['adapter'] == 'bigquant_v1' and len(caps) == 1 and isinstance(caps[0].value, ast.Constant)
            and type(caps[0].value.value) in (int, float) and cap < caps[0].value.value <= 1):
        options.append({'id': 'confirmed_cap', 'finding': 'constraint_tradeoffs.confirmed_cap',
            'title': '将代码单票上限收紧到已确认上限',
            'reason': f'代码为{caps[0].value.value:.1%}，本次用户确认上限为{cap:.1%}；只收紧，不放宽。',
            'lines': [caps[0].lineno], 'tradeoff': '只修改代码的上限设置，不保证存量仓位已压回上限；需原平台重测。'})
    return {'options': options, 'unavailable': unavailable, '_buys': candidates, '_caps': caps}


def propose(bundle, result, selected, *, minimum_buy_notional=None):
    inspection = inspect(bundle, result)
    options = {o['id']: o for o in inspection['options']}
    if not selected or set(selected) - options.keys():
        raise ValueError('请选择当前代码和证据支持的治理修改')
    source = bundle['code']['source']
    lines = source.splitlines(keepends=True)
    byte_lines = [line.encode('utf-8') for line in lines]
    offsets = [0]
    for line in byte_lines:
        offsets.append(offsets[-1] + len(line))
    edits, changes = [], []
    newline = '\r\n' if '\r\n' in source else '\n'

    def replace(node, text):
        start = offsets[node.lineno-1] + node.col_offset
        end = offsets[node.end_lineno-1] + node.end_col_offset
        edits.append((start, end, text.encode('utf-8')))

    if 'small_buy_guard' in selected:
        amount = float(minimum_buy_notional) if minimum_buy_notional is not None else options['small_buy_guard']['default_threshold']
        if not math.isfinite(amount) or not 0 < amount <= 1_000_000_000:
            raise ValueError('最低买入金额须为大于0、不超过10亿元的有限数')
        # Round upward so the advertised threshold cannot undercut the chosen amount.
        amount = math.ceil(amount * 100) / 100
        for statement in inspection['_buys']:
            call = statement.value
            indent = byte_lines[statement.lineno-1][:statement.col_offset].decode('utf-8')
            old = ast.get_source_segment(source, statement)
            note = f'# TemperQ [costs.minimum_commission] 费用控制候选：小于{amount:.2f}元的买入暂不提交；需原平台回测。'
            replace(statement, note+newline+indent+f'if {call.args[1].id} >= {amount!r}:'+newline+indent+'    '+old)
        changes.append({**options['small_buy_guard'], 'minimum_buy_notional': amount})
    if 'confirmed_cap' in selected:
        node = inspection['_caps'][0]
        indent = byte_lines[node.lineno-1][:node.col_offset].decode('utf-8')
        value = result['settings']['max_weight']
        note = '# TemperQ [constraint_tradeoffs.confirmed_cap] 按本次确认收紧单票上限；存量超限仍需处理。'
        replace(node, note+newline+indent+f'context.max_cash_per_instrument = {value!r}')
        changes.append({**options['confirmed_cap'], 'max_weight': value})
    raw = source.encode('utf-8')
    for start, end, replacement in sorted(edits, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    optimized = raw.decode('utf-8')
    if _tree(optimized) is None or optimized == source:
        raise ValueError('代码改写未通过静态语法核对，未保存候选')
    # With just these allowlisted edits, untouched factor/SQL/ranking text is retained.
    code_source = next(s for s in bundle['sources'] if s['kind'] == 'code')
    encoding = code_source.get('encoding', 'utf-8-sig')
    if encoding == 'gb18030' and not re.search(r'coding[:=]\s*[-\w.]+', '\n'.join(optimized.splitlines()[:2])):
        # Vendor TXT exports need no Python coding cookie; our downloadable PY does.
        encoding = 'utf-8-sig'
    try:
        ast.parse(optimized.encode(encoding))
    except (SyntaxError, UnicodeError, LookupError) as exc:
        raise ValueError('代码下载编码与原文声明不一致，请先统一源码编码') from exc
    diff = ''.join(unified_diff(source.splitlines(keepends=True), optimized.splitlines(keepends=True),
                                fromfile='original.py', tofile='optimized_candidate.py'))
    proposal = {'schema_version': 'governance_code_candidate_v1', 'input_digest': object_digest(bundle),
        'report_digest': result['result_digest'], 'status': 'awaiting_original_platform_backtest',
        'original_sha256': sha256(source.encode('utf-8')).hexdigest(),
        'optimized_sha256': sha256(optimized.encode('utf-8')).hexdigest(),
        'source': source, 'optimized_source': optimized, 'encoding': encoding,
        'diff': diff, 'changes': changes, 'code_executed': False, 'performance_improvement': None,
        'meaning': '按审计发现生成的治理代码候选；未用历史收益选阈值，未重放或证明收益改善。选股、因子与打分原文保留，需回原平台重新回测。'}
    proposal['candidate_digest'] = object_digest(proposal)
    return proposal
