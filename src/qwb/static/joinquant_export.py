# -*- coding: utf-8 -*-
"""TemperQ 导出脚本 · 聚宽研究环境

用途：把聚宽一次回测的成交、每日持仓和账户权益导出成 TemperQ 通用格式的三个 CSV，
再连同策略代码一起上传到 TemperQ 做策略审计。

用法（在聚宽「研究环境」新建一个 Notebook，粘贴本文件全部内容）：
  1. 把下面 BACKTEST_ID 换成你的回测 ID（回测详情页地址栏或「回测列表」里能看到）。
  2. 运行。完成后研究环境的文件列表里会出现：
       temperq_trades.csv     逐笔成交（必需）
       temperq_holdings.csv   每日持仓（推荐）
       temperq_account.csv    每日权益与现金（推荐）
  3. 下载这三个文件，再加上策略代码（.py 或 .txt），一起上传到 TemperQ，平台选「自动识别」或「其他 / 标准 CSV」。

说明：
  - 本脚本只读取回测结果，不改动你的策略，也不上传任何数据。
  - 聚宽研究环境的接口字段可能随版本变化。脚本会打印每类记录的字段名；
    如果运行报错或导出为空，请把打印出来的内容发给 TemperQ 维护者。
  - 价格口径以你回测的设置为准。建议回测时开启 set_option('use_real_price', True)，
    否则成交价是复权价，股数和金额会和实盘对不上。
"""

BACKTEST_ID = '请替换为你的回测ID'
OUT_PREFIX = 'temperq_'

import csv


# ---------------------------------------------------------------- conversion
# 这部分是纯函数，不依赖聚宽环境，便于离线测试。

def _date(value):
    text = str(value).strip()
    return text[:10]


def _time(value):
    text = str(value).strip()
    return text[11:19] if len(text) >= 19 else ''


def _first(record, *keys, default=None):
    for key in keys:
        if key in record and record[key] not in (None, ''):
            return record[key]
    return default


def _side(order):
    """聚宽订单方向：action 为 open/close，side 为 long/short；也兼容 is_buy、buy/sell。"""
    if 'is_buy' in order:
        return 'buy' if order['is_buy'] else 'sell'
    action = str(_first(order, 'action', 'direction', default='')).lower()
    side = str(_first(order, 'side', default='long')).lower()
    if side not in ('long', ''):
        return None  # 只支持 A 股只做多
    if action in ('open', 'buy'):
        return 'buy'
    if action in ('close', 'sell'):
        return 'sell'
    return None


def convert(orders, positions, balances):
    """返回 (trades, holdings, account, notes)。每个是 dict 行列表。"""
    trades, holdings, account, notes = [], [], [], []
    skipped = 0
    for order in orders or []:
        quantity = _first(order, 'filled', 'filled_amount', 'amount')
        price = _first(order, 'price', 'avg_cost', 'avg_price')
        side = _side(order)
        if side is None or not quantity or not price:
            skipped += 1
            continue
        status = str(_first(order, 'status', default='')).lower()
        if status in ('canceled', 'cancelled', 'rejected') and not _first(order, 'filled'):
            skipped += 1
            continue
        stamp = _first(order, 'time', 'add_time', 'datetime', default='')
        trades.append({
            'date': _date(stamp), 'time': _time(stamp),
            'code': _first(order, 'security', 'code'),
            'side': side, 'quantity': int(round(float(quantity))), 'price': float(price),
            'fee': float(_first(order, 'commission', 'fee', default=0) or 0),
            'order_id': str(_first(order, 'order_id', 'id', default='')),
        })
    if skipped:
        notes.append('跳过 %d 条未成交、已撤或方向无法识别的订单' % skipped)

    for row in positions or []:
        amount = _first(row, 'amount', 'total_amount', 'quantity')
        side = str(_first(row, 'side', default='long')).lower()
        if not amount or side not in ('long', ''):
            continue
        holdings.append({
            'date': _date(_first(row, 'time', 'date', default='')),
            'code': _first(row, 'security', 'code'),
            'quantity': int(round(float(amount))),
            'price': float(_first(row, 'price', 'close', default=0) or 0) or '',
        })

    for row in balances or []:
        equity = _first(row, 'total_value', 'total_assets', 'portfolio_value')
        cash = _first(row, 'cash', 'available_cash', 'aval_cash')
        if equity is None or cash is None:
            continue
        account.append({'date': _date(_first(row, 'time', 'date', default='')),
                        'equity': float(equity), 'cash': float(cash)})
    if not account:
        notes.append('没有取到每日权益与现金，temperq_account.csv 不会生成；不影响上传，但回撤与归因会改用近似')
    for rows in (trades, holdings, account):
        rows.sort(key=lambda r: (r['date'], r.get('time', ''), str(r.get('code', ''))))
    return trades, holdings, account, notes


def write(path, rows, columns):
    with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------- JoinQuant
def _call(obj, name):
    method = getattr(obj, name, None)
    if method is None:
        print('提示：回测对象没有 %s()，跳过' % name)
        return []
    try:
        return list(method())
    except Exception as exc:  # 只打印，不中断其他导出
        print('提示：%s() 调用失败：%s' % (name, exc))
        return []


def main():
    backtest = get_backtest(BACKTEST_ID)  # noqa: F821 — 聚宽研究环境内置
    orders = _call(backtest, 'get_orders')
    positions = _call(backtest, 'get_positions')
    balances = _call(backtest, 'get_balances')
    for label, rows in (('订单', orders), ('持仓', positions), ('账户', balances)):
        print('%s：%d 条；字段：%s' % (label, len(rows), sorted(rows[0].keys()) if rows else '无'))
    trades, holdings, account, notes = convert(orders, positions, balances)
    if not trades:
        raise SystemExit('没有导出任何成交。请检查 BACKTEST_ID，并把上面打印的字段名发给维护者。')
    write(OUT_PREFIX + 'trades.csv', trades, ['date', 'time', 'code', 'side', 'quantity', 'price', 'fee', 'order_id'])
    if holdings:
        write(OUT_PREFIX + 'holdings.csv', holdings, ['date', 'code', 'quantity', 'price'])
    if account:
        write(OUT_PREFIX + 'account.csv', account, ['date', 'equity', 'cash'])
    print('已导出：成交 %d 笔（%s 至 %s），持仓 %d 行，账户 %d 天'
          % (len(trades), trades[0]['date'], trades[-1]['date'], len(holdings), len(account)))
    for note in notes:
        print('注意：' + note)
    print('下一步：下载 temperq_*.csv 和你的策略代码，上传到 TemperQ。')


# Runs only inside the JoinQuant research environment, where get_backtest is built in.
if 'get_backtest' in globals():
    main()
