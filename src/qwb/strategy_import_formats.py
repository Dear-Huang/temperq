"""Read-only adapters for JoinQuant position exports and minimal CSV tables."""
from __future__ import annotations

from collections import defaultdict
import csv
from hashlib import sha256
import io
import re

from .strategy_imports import (LOG_LINE, is_platform_log, SCHEMA, MAX_ROWS, _ref, date_value, decode_source,
                               inspect_code, numeric)
from .securities import security_code as stock_code
from .audit_fee_units import order_fields


def code_value(value):
    text = str(value).strip().upper()
    match = re.search(r'\d{6}\.(?:XSHG|XSHE|SH|SZ|BJ)', text) or re.search(r'[A-Z]{1,2}\d{3,4}\.[A-Z]{3,4}', text)
    if match:
        text = match[0]
    return stock_code(text.replace('.XSHG', '.SH').replace('.XSHE', '.SZ'))


def table(source):
    text, encoding = decode_source(source)
    rows = list(csv.reader(io.StringIO(text), strict=True))
    if not rows or len(rows) > MAX_ROWS + 1:
        raise ValueError('CSV 为空或超过行数上限')
    header = [v.strip().lstrip('\ufeff') for v in rows[0]]
    if {'order_book_id', 'last_quantity', 'transaction_cost'} <= set(header) and header.count('datetime') == 2:
        header[len(header) - 1 - header[::-1].index('datetime')] = 'datetime_2'   # native RQAlpha duplicate
    # RiceQuant pads each value with a leading space after the comma.
    data = [(i, [x.strip() for x in r]) for i, r in enumerate(rows[1:], 2) if any(x.strip() for x in r)]
    # The verified JoinQuant export omits the total-assets heading while retaining
    # the values immediately before the final position-weight column.
    repaired = False
    if {'日期', '标的', '数量'} <= set(header) and header[-1:] == ['仓位占比']:
        target_col = header.index('标的')
        if any(len(r) <= target_col for _, r in data):
            raise ValueError('CSV 数据列数不足，无法识别标的')
        stocks = [r for _, r in data if r[target_col].strip().lower() != 'cash']
        if stocks and all(len(r) == len(header) + 1 for r in stocks) and all(
                len(r) == len(header) + 1 or (r[target_col].strip().lower() == 'cash' and len(r) == len(header))
                for _, r in data):
            for _, r in data:
                if len(r) == len(header):
                    r.insert(-1, '')
            header.insert(-1, '总资产')
            repaired = True
    if len(set(header)) != len(header) or any(len(r) != len(header) for _, r in data):
        raise ValueError('CSV 表头重复或列数不一致；无法安全识别多出的字段')
    return header, [(i, dict(zip(header, r))) for i, r in data], encoding, repaired


def _receivable_dividends(result):
    """A rise in 应收股息 is a dividend the account recorded on its ex-date. The amount is a fact;
    only its split across names held the day before is estimated, by market value."""
    held = defaultdict(dict)
    for p in result['positions']:
        held[p['date']][p['code']] = p['market_value']
    snaps = result['snapshots']
    booked = 0
    for prev, snap in zip(snaps, snaps[1:]):
        rise = snap.get('receivable_dividend', 0) - prev.get('receivable_dividend', 0)
        names = held.get(prev['date'], {})
        total = sum(names.values())
        if rise <= .005 or total <= 0:
            continue
        for code, value in sorted(names.items()):
            result['actions'].append({'date': snap['date'], 'code': code, 'quantity_change': 0,
                'cash': rise * value / total, 'kind': 'cash_dividend', 'source': 'receivable_dividend',
                'evidence': f"应收股息由 {prev.get('receivable_dividend', 0):g} 增至 {snap['receivable_dividend']:g}",
                'ref': snap['ref']})
        booked += 1
    if booked:
        result['quality']['receivable_dividend_days'] = booked


def _settle_rounded_prices(result):
    """RiceQuant prints fill prices rounded after slippage. When a day's only unexplained cash
    gap is within the rounding of that day's prices, book the fills at the amount actually paid."""
    by_day = defaultdict(list)
    for fill in result['fills']:
        by_day[fill['date']].append(fill)
    snaps = result['snapshots']
    adjusted = 0
    for prev, snap in zip(snaps, snaps[1:]):
        fills = by_day.get(snap['date'])
        if not fills:
            continue
        flow = sum(f['notional'] * (1 if f['side'] == 'buy' else -1) + f['fee'] for f in fills)
        flow -= sum(x['cash'] for x in result['actions'] if x['date'] == snap['date'])
        gap = (prev['cash'] - flow) - snap['cash']          # positive: less cash than the prices imply
        # Half of the last printed decimal on every share traded bounds the rounding.
        bound = sum(f['quantity'] * .5 * 10 ** -_decimals(f['price']) for f in fills)
        if not 1e-6 < abs(gap) <= bound + .01:
            continue
        traded = sum(f['quantity'] for f in fills)
        for f in fills:
            share = gap * f['quantity'] / traded
            f['notional'] += share if f['side'] == 'buy' else -share
            f['notional_source'] = 'account_cash'
        adjusted += len(fills)
    if adjusted:
        result['quality']['rounded_price_fills'] = adjusted


def _decimals(price):
    text = f"{price:.6f}".rstrip('0')
    return len(text.split('.')[1]) if '.' in text else 0


def detect_adapter(files):
    found = set()
    generic_account = False
    for source in files:
        if source.name.lower().endswith(('.py', '.txt')):
            continue
        text, _ = decode_source(source)
        header = next(csv.reader(io.StringIO(text), strict=True), [])
        fields = {v.strip().lstrip('\ufeff') for v in header}
        generic_account |= {'date', 'equity', 'cash'} <= fields
        if {'标的', '多空', '数量'} <= fields or {'标的', '交易类型', '成交数量', '手续费'} <= fields:
            found.add('joinquant')
        elif ({'合约代码', '仓位', '最新价'} <= fields or {'账户类型', '可用资金', '总权益'} <= fields
              or {'合约代码', '买/卖', '成交量', '成交价'} <= fields
              or {'event', 'order_id', 'status', 'filled_quantity'} <= fields
              or {'order_book_id', 'last_quantity', 'transaction_cost'} <= fields
              or {'order_book_id', 'quantity', 'last_price', 'market_value'} <= fields
              or {'cash', 'transaction_cost', 'market_value', 'total_value'} <= fields):
            found.add('ricequant')
        elif {'买/卖', '成交价'} <= fields or {'持仓均价', '持仓市值'} <= fields:
            found.add('bigquant')
        elif {'code', 'date', 'quantity'} <= fields:
            found.add('generic')
    if found == {'joinquant', 'generic'}:
        return 'joinquant'  # Standard CSV trades may supplement a vendor holdings export.
    if len(found) > 1:
        raise ValueError('不同平台的交易或持仓不能混为同一次回测，请分开导入')
    return next(iter(found), 'generic' if generic_account else 'code_only')


def quantity(value):
    cleaned = str(value).strip().replace(',', '')
    cleaned = re.sub(r'(股|份)$', '', cleaned)
    amount = numeric(cleaned, '股数')
    if not amount.is_integer():
        raise ValueError('股票数量须为整数股')
    return int(amount)


def infer_changes(result):
    """Net changes only: unknown executions, fees and intraday round trips stay unknown."""
    positions = defaultdict(dict)
    for row in result['positions']:
        positions[row['date']][row['code']] = row
    days = sorted({s['date'] for s in result['snapshots']} | set(positions))
    for before, after in zip(days, days[1:]):
        old, new = positions[before], positions[after]
        for code in sorted(old.keys() | new.keys()):
            previous, current = old.get(code, {}), new.get(code, {})
            delta = current.get('quantity', 0) - previous.get('quantity', 0)
            if not delta:
                continue
            ref_price = current.get('price') or previous.get('price')
            if not ref_price or ref_price <= 0:
                continue
            result['fills'].append({'date': after, 'time': '', 'code': code,
                'side': 'buy' if delta > 0 else 'sell', 'quantity': abs(delta),
                'price': ref_price, 'notional': abs(delta) * ref_price, 'fee': 0.,
                'fee_reported': False, 'inferred': True, 'kind': 'net_position_change',
                'price_basis': 'current_close_reference' if current else 'previous_close_reference',
                'ref': {'source': 'positions', 'row': (current or previous)['ref']['row'],
                        'previous_date': before, 'current_date': after}})
    result['quality']['fills_inferred'] = True
    result['quality']['inference_note'] = ('由持仓推算净变动；股数变化可能包括公司行动，不能恢复日内往返、成交价或真实费用。'
        '首个持仓快照不虚构买入；退出证券只用前日收盘价作金额参考。')


def other_platform(files, *, adapter, name, opening_cash=None):
    result = {'schema_version': SCHEMA, 'adapter': adapter + '_v1', 'name': name,
        'opening': {'kind': 'empty' if opening_cash is not None else 'first_close', 'cash': opening_cash},
        'fills': [], 'positions': [], 'snapshots': [], 'actions': [], 'logs': [],
        'external_attribution': [], 'quote_observations': [], 'scores': [], 'prices': [], 'code': {}, 'sources': [],
        'quality': {'fills_inferred': False, 'holdings_header_repaired': False,
                    'price_basis': 'unconfirmed', 'snapshots_complete': False}}
    seen, cash, assets = set(), {}, {}
    for source in files:
        text, encoding = decode_source(source)
        if source.name.lower().endswith('.txt') and is_platform_log(text):
            kind = 'logs'
            for index, line in enumerate(text.splitlines(), 1):
                match = LOG_LINE.match(line)
                if match:
                    result['logs'].append({'时间': match[1], '级别': match[2], '内容': match[3].strip(),
                                           'ref': _ref(kind, index)})
                elif result['logs']:
                    result['logs'][-1]['内容'] += chr(10) + line  # continuation of a multi-line message
            row_count = len(result['logs'])
        elif source.name.lower().endswith(('.py', '.txt')):
            kind = 'code'
            result['code'] = {**inspect_code(text), 'source': text}
            row_count = len(text.splitlines())
        else:
            header, rows, encoding, repaired = table(source)
            result['quality']['holdings_header_repaired'] |= repaired
            fields = set(header)
            if {'标的', '多空', '数量'} <= fields:
                kind = 'positions'
                for index, row in rows:
                    day = date_value(row['日期'], '持仓日期')
                    target = row['标的'].strip()
                    if target.lower() == 'cash':
                        cash[day] = numeric(row['市值/价值'], '现金', nonnegative=False)
                        continue
                    if row['多空'].strip() not in {'多', ''}:
                        raise ValueError('首版只支持 A 股只做多持仓')
                    q = quantity(row['数量'])
                    if q == 0:
                        continue
                    price, value = numeric(row['收盘价/结算价'], '收盘价'), numeric(row['市值/价值'], '市值')
                    result['positions'].append({'date': day, 'code': code_value(target),
                        'quantity': q, 'price': price, 'market_value': value, 'ref': _ref(kind, index)})
                    if row.get('总资产', '').strip():
                        equity = numeric(row['总资产'], '总资产')
                        if day in assets and abs(assets[day] - equity) > .05:
                            raise ValueError('同日总资产不一致')
                        assets[day] = equity
            elif {'标的', '交易类型', '成交数量', '手续费'} <= fields:
                # JoinQuant 交易详情: one row per order with its filled part; sells carry negative quantities.
                kind = 'fills'
                for index, row in rows:
                    q = quantity(row['成交数量'].strip().lstrip('-'))
                    if q == 0:
                        continue  # cancelled with nothing filled
                    side = {'买': 'buy', '卖': 'sell'}.get(row['交易类型'].strip())
                    if side is None:
                        raise ValueError('聚宽交易类型须为 买/卖')
                    price = numeric(row['成交价'], '成交价')
                    result['fills'].append({'date': date_value(row['日期'], '成交日期'),
                        'code': code_value(row['标的']), 'quantity': q, 'price': price, 'side': side,
                        'notional': q * price, 'fee': numeric(row['手续费'], '手续费'),
                        'time': row.get('委托时间', ''), 'ref': _ref(kind, index), **order_fields({})})
            elif {'合约代码', '买/卖', '成交量', '成交价'} <= fields:
                # RiceQuant 交易详情: one row per fill; 费用 is the fee charged on that fill.
                kind = 'fills'
                for index, row in rows:
                    side = {'买': 'buy', '卖': 'sell'}.get(row['买/卖'].strip())
                    if side is None:
                        raise ValueError('米筐买/卖须为 买 或 卖')
                    if row.get('开/平', '').strip() not in {'开', '平', ''}:
                        raise ValueError('首版只支持 A 股股票买卖')
                    q, price = quantity(row['成交量']), numeric(row['成交价'], '成交价')
                    if q <= 0 or price <= 0:
                        raise ValueError('成交股数和价格须为正')
                    stamp = row['时间'].strip()
                    result['fills'].append({'date': date_value(stamp, '成交日期'),
                        'code': code_value(row['合约代码']), 'quantity': q, 'price': price, 'side': side,
                        'notional': q * price, 'fee': numeric(row.get('费用', '') or '0', '费用'),
                        'time': stamp[11:19], 'ref': _ref(kind, index), **order_fields({})})
            elif {'合约代码', '仓位', '最新价'} <= fields:
                # RiceQuant 持仓详情: daily closing holdings.
                kind = 'positions'
                for index, row in rows:
                    if row.get('多/空', '--') not in {'--', '多', ''}:
                        raise ValueError('首版只支持 A 股只做多持仓')
                    q = quantity(row['仓位'])
                    if q == 0:
                        continue
                    price = numeric(row['最新价'], '收盘价')
                    result['positions'].append({'date': date_value(row['时间'], '持仓日期'),
                        'code': code_value(row['合约代码']), 'quantity': q, 'price': price,
                        'market_value': numeric(row['市值'], '市值'), 'ref': _ref(kind, index)})
            elif {'账户类型', '可用资金', '总权益'} <= fields:
                # RiceQuant 账户详情: equity, cash and the day's total fee.
                kind = 'snapshots'
                for index, row in rows:
                    equity = numeric(row['总权益'], '总权益')
                    current_cash = numeric(row['可用资金'], '可用资金', nonnegative=False)
                    # Local RQAlpha adds dividends declared but not yet paid; they are cash owed to the account.
                    receivable = numeric(row.get('应收股息', '') or '0', '应收股息')
                    result['snapshots'].append({'date': date_value(row['日期'], '账户日期'),
                        'equity': equity, 'cash': current_cash + receivable, 'market_value': equity - current_cash - receivable,
                        'receivable_dividend': receivable,
                        'reported_day_fee': numeric(row.get('费用', '') or '0', '费用'), 'ref': _ref(kind, index)})
            elif {'date', 'code', 'quantity', 'side', 'price', 'fee'} <= fields:
                kind = 'fills'
                for index, row in rows:
                    side = row['side'].strip().lower()
                    side = {'买入': 'buy', '卖出': 'sell'}.get(side, side)
                    if side not in {'buy', 'sell'}:
                        raise ValueError('成交方向须为 buy/sell 或买入/卖出')
                    q, price = quantity(row['quantity']), numeric(row['price'], '成交价')
                    if q <= 0 or price <= 0:
                        raise ValueError('成交股数和价格须为正')
                    result['fills'].append({'date': date_value(row['date'], '成交日期'),
                        'code': code_value(row['code']), 'quantity': q, 'price': price, 'side': side,
                        'notional': q * price, 'fee': numeric(row['fee'], '费用'),
                        'time': row.get('time', ''), 'ref': _ref(kind, index), **order_fields(row)})
            elif {'date', 'code', 'quantity'} <= fields:
                kind = 'positions'
                for index, row in rows:
                    q = quantity(row['quantity'])
                    price = numeric(row['price'], '持仓价') if row.get('price', '').strip() else None
                    result['positions'].append({'date': date_value(row['date'], '持仓日期'),
                        'code': code_value(row['code']), 'quantity': q, 'price': price,
                        'market_value': q * price if price is not None else None, 'ref': _ref(kind, index)})
            elif {'date', 'equity', 'cash'} <= fields:
                kind = 'snapshots'
                for index, row in rows:
                    equity, current_cash = numeric(row['equity'], '权益'), numeric(row['cash'], '现金', nonnegative=False)
                    result['snapshots'].append({'date': date_value(row['date'], '账户日期'),
                        'equity': equity, 'cash': current_cash, 'market_value': equity - current_cash, 'ref': _ref(kind, index)})
            elif {'order_book_id', 'last_quantity', 'transaction_cost'} <= fields:
                # Native RQAlpha trades.csv: one row per fill; transaction_cost = commission + tax.
                kind = 'fills'
                for index, row in rows:
                    side = {'BUY': 'buy', 'SELL': 'sell'}.get(row['side'].strip().upper())
                    if side is None:
                        raise ValueError(f'{source.name} 第 {index} 行：side 须为 BUY 或 SELL')
                    if row.get('position_effect', '').strip().upper() not in {'OPEN', 'CLOSE', ''}:
                        raise ValueError('首版只支持 A 股股票买卖')
                    q, price = quantity(row['last_quantity']), numeric(row['last_price'], '成交价')
                    if q <= 0 or price <= 0:
                        raise ValueError('成交股数和价格须为正')
                    stamp = (row.get('trading_datetime') or row['datetime']).strip()
                    result['fills'].append({'date': date_value(stamp, '成交日期'),
                        'code': code_value(row['order_book_id']), 'quantity': q, 'price': price, 'side': side,
                        'notional': q * price, 'fee': numeric(row['transaction_cost'], '费用'),
                        'tax': numeric(row.get('tax') or '0', '税费'), 'time': stamp[11:19], 'ref': _ref(kind, index),
                        **order_fields({'order_id': row.get('order_id', ''), 'fill_id': row.get('exec_id', '')})})
            elif {'order_book_id', 'quantity', 'last_price', 'market_value'} <= fields:
                # Native RQAlpha stock_positions.csv: daily closing holdings.
                kind = 'positions'
                for index, row in rows:
                    q = quantity(row['quantity'])
                    if q == 0:
                        continue
                    result['positions'].append({'date': date_value(row['date'], '持仓日期'),
                        'code': code_value(row['order_book_id']), 'quantity': q, 'price': numeric(row['last_price'], '收盘价'),
                        'market_value': numeric(row['market_value'], '市值'), 'ref': _ref(kind, index)})
            elif {'cash', 'transaction_cost', 'market_value', 'total_value'} <= fields:
                # Native RQAlpha stock_account.csv. total_value may exceed cash + market value by dividends receivable.
                kind = 'snapshots'
                for index, row in rows:
                    equity = numeric(row['total_value'], '总权益')
                    current_cash = numeric(row['cash'], '现金', nonnegative=False)
                    mv = numeric(row['market_value'], '市值')
                    receivable = round(equity - current_cash - mv, 6)
                    result['snapshots'].append({'date': date_value(row['date'], '账户日期'),
                        'equity': equity, 'cash': current_cash + max(receivable, 0.), 'market_value': mv,
                        'receivable_dividend': max(receivable, 0.),
                        'reported_day_fee': numeric(row.get('transaction_cost') or '0', '费用'), 'ref': _ref(kind, index)})
            elif {'date', 'cash', 'total_value', 'unit_net_value'} <= fields or {'date', 'count', 'mean', 'std'} <= fields:
                # Native RQAlpha portfolio.csv / positions_weight.csv: duplicates of the account, kept as sources only.
                kind = 'platform_returns' if 'unit_net_value' in fields else 'auxiliary'
                if kind == 'platform_returns':
                    first = None
                    result['platform_returns'] = []
                    for index, row in rows:
                        value = numeric(row['unit_net_value'], '单位净值')
                        result['platform_returns'].append({'date': date_value(row['date'], '净值日期'),
                            'strategy_pct': (value - 1) * 100, 'ref': _ref(kind, index)})
            elif {'event', 'order_id', 'status', 'filled_quantity'} <= fields:
                # RQAlpha 委托详情: one row per order event; keep the final state of each order.
                kind = 'orders'
                final = {}
                for index, row in rows:
                    oid = row['order_id'].strip()
                    status = row['status'].strip().replace('ORDER_STATUS.', '').lower()
                    message = row.get('message', '').strip()
                    reason = ('无成交量' if '无成交量' in message else
                              '超过当根成交量上限' if ('percent of current bar volume' in message or '超过了成交量' in message) else
                              '可用资金不足' if '资金不足' in message else
                              '涨跌停' if ('涨停' in message or '跌停' in message or 'limit' in message.lower()) else
                              '停牌' if '停牌' in message or 'suspend' in message.lower() else
                              '其他原因' if status in {'cancelled', 'rejected'} else '')
                    cash = re.search(r'当前资金[:：]\s*([\d.]+).*?所需资金[:：]\s*([\d.]+)', message)
                    final[oid] = {'date': date_value(row['time'], '委托日期'), 'time': row['time'].strip()[11:19],
                        'code': code_value(row['code']), 'side': 'buy' if 'BUY' in row['side'].upper() else 'sell',
                        'quantity': quantity(row['quantity']), 'filled': quantity(row['filled_quantity'] or '0'),
                        'status': status, 'reason': reason, 'order_id': oid, 'ref': _ref(kind, index),
                        'message': message[:200],
                        **({'available_cash': float(cash[1]), 'required_cash': float(cash[2])} if cash else {})}
                result['orders'] = sorted(final.values(), key=lambda o: (o['date'], o['time'], o['order_id']))
            elif {'datetime', 'rank', 'order_book_id'} <= fields or {'date', 'code', 'rank'} <= fields:
                # The strategy's own daily ranking: rank per candidate, optionally which ones it selected.
                kind = 'scores'
                for index, row in rows:
                    stamp = (row.get('datetime') or row.get('date') or '').strip()
                    rank = numeric(row['rank'], '排名')
                    if rank < 1 or not rank.is_integer():
                        raise ValueError(f'{source.name} 第 {index} 行：排名须为正整数')
                    flag = (row.get('selected') or '').strip().lower()
                    raw = (row.get('score') or '').strip()
                    result['scores'].append({'date': date_value(stamp, '排名日期'),
                        'code': code_value(row.get('order_book_id') or row['code']),
                        'score': numeric(raw, '打分', nonnegative=False) if raw else -rank, 'rank': int(rank),
                        'selected': flag in {'true', '1', 'yes', '是'} if flag else None,
                        'available_at': stamp[11:19] if len(stamp) > 10 else row.get('available_at', ''),
                        'ref': _ref(kind, index)})
            elif {'date', 'code', 'close'} <= fields and 'quantity' not in fields:
                # Candidate closing prices, for checking the strategy's premise across its whole pool.
                kind = 'prices'
                for index, row in rows:
                    result['prices'].append({'date': date_value(row['date'], '价格日期'), 'code': code_value(row['code']),
                                             'close': numeric(row['close'], '收盘价'), 'ref': _ref(kind, index)})
            elif {'date', 'code', 'score'} <= fields:
                kind = 'scores'
                for index, row in rows:
                    result['scores'].append({'date': date_value(row['date'], '打分日期'),
                        'code': code_value(row['code']), 'score': numeric(row['score'], '打分', nonnegative=False),
                        'available_at': row.get('available_at', ''), 'ref': _ref(kind, index)})
            elif {'时间', '策略收益', '基准收益'} <= fields:
                # JoinQuant 收益详情: the platform's own cumulative return, kept to cross-check equity.
                kind = 'platform_returns'
                result['platform_returns'] = [{'date': date_value(row['时间'], '收益日期'),
                    'strategy_pct': numeric(row['策略收益'], '策略收益', nonnegative=False),
                    'ref': _ref(kind, index)} for index, row in rows]
            elif {'时间', '级别', '内容'} <= fields:
                kind = 'logs'
                result['logs'] = [{**row, 'ref': _ref(kind, i)} for i, row in rows]
            else:
                raise ValueError(source.name + '：未识别的表头，请使用平台原始导出或页面所列最小格式')
            row_count = len(rows)
        if kind in seen:
            raise ValueError('每种材料只接收一个文件，请先合并同类导出')
        seen.add(kind)
        result['sources'].append({'kind': kind, 'name': source.name, 'encoding': encoding,
            'sha256': sha256(source.data).hexdigest(), 'bytes': len(source.data), 'rows': row_count})
    if adapter == 'joinquant':
        marked = defaultdict(float)
        for p in result['positions']:
            marked[p['date']] += p['market_value']
        for day in sorted(cash.keys() | assets.keys()):
            if day not in cash:
                continue
            result['snapshots'].append({'date': day, 'cash': cash[day],
                'equity': assets.get(day, cash[day] + marked[day]),
                'market_value': marked[day], 'ref': {'source': 'positions', 'row': 0}})
    for key in ('positions', 'snapshots', 'fills', 'scores'):
        result[key].sort(key=lambda r: r['date'])
        identities = [(r['date'], r.get('code', '')) for r in result[key]]
        if key != 'fills' and len(identities) != len(set(identities)):
            raise ValueError('同日持仓、账户或打分重复')
    if adapter == 'ricequant' and result['snapshots']:
        _receivable_dividends(result)
    if adapter == 'ricequant' and result['snapshots'] and result['fills']:
        _settle_rounded_prices(result)
    result['quality']['snapshots_complete'] = bool(result['snapshots'])
    result['quality']['negative_cash_days'] = sum(s['cash'] < 0 for s in result['snapshots'])
    if 'fills' not in seen and result['positions']:
        infer_changes(result)
    return result
