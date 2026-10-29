# -*- coding: utf-8 -*-
"""TemperQ 补材料脚本 · 聚宽 / 米筐研究环境

回测跑完后，在研究环境运行本文件，补齐审计需要、但平台不默认导出的两份材料：

  temperq_prices.csv   候选池收盘价（检验策略前提用）：打分里出现过的每只标的、每个打分日
  temperq_minutes.csv  成交日分钟价（检验成交时点用）：每个有成交的日子、每只成交标的的几个时点

需要的输入（放在研究环境同一目录）：
  temperq_scores.csv   由 temperq_recorder.py 在回测里记下（没有则跳过候选池收盘价）
  trades.csv           成交记录，至少有 date、code 两列（TemperQ 标准格式即可；没有则跳过分钟价）

只读行情，不改任何文件以外的东西。
"""
import csv
import os

TIMES = ['09:31', '09:35', '09:45', '10:00', '10:30', '11:00', '13:30', '14:00', '14:30', '14:50']


def _rows(name):
    if not os.path.exists(name):
        return []
    with open(name, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def platform_code(code):
    """600000.SH -> 600000.XSHG；聚宽、米筐都用 XSHG/XSHE。"""
    code = code.strip().upper()
    if code.endswith('.SH'):
        return code[:-3] + '.XSHG'
    if code.endswith('.SZ'):
        return code[:-3] + '.XSHE'
    return code


def needed(scores, trades):
    """(打分日 × 标的) 与 (成交日 × 标的)。纯函数，便于离线测试。"""
    price_keys = sorted({(r['date'][:10], platform_code(r['code'])) for r in scores})
    minute_keys = sorted({(r['date'][:10], platform_code(r['code'])) for r in trades})
    return price_keys, minute_keys


def _get_price(code, start, end, frequency):
    """聚宽 get_price 与米筐 get_price 的参数不同，这里统一。"""
    try:                                                    # 聚宽
        return get_price(code, start_date=start, end_date=end, frequency=frequency,
                         fields=['open', 'close', 'volume'], skip_paused=False, fq='post' if frequency == 'daily' else None)
    except TypeError:                                       # 米筐
        return get_price(code, start_date=start, end_date=end, frequency='1d' if frequency == 'daily' else '1m',
                         fields=['open', 'close', 'volume'], adjust_type='post' if frequency == 'daily' else 'none')


def main():
    scores, trades = _rows('temperq_scores.csv'), _rows('trades.csv') or _rows('temperq_trades.csv')
    price_keys, minute_keys = needed(scores, trades)
    if price_keys:
        by_code = {}
        for day, code in price_keys:
            by_code.setdefault(code, []).append(day)
        with open('temperq_prices.csv', 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f); w.writerow(['date', 'code', 'close'])
            for code, days in sorted(by_code.items()):
                frame = _get_price(code, min(days), max(days), 'daily')
                wanted = set(days)
                for stamp, row in frame.iterrows():
                    day = str(stamp)[:10]
                    if day in wanted and row['close'] == row['close']:
                        w.writerow([day, code, row['close']])
        print('temperq_prices.csv', len(price_keys), '个打分日×标的')
    else:
        print('没有 temperq_scores.csv，跳过候选池收盘价')
    if minute_keys:
        with open('temperq_minutes.csv', 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f); w.writerow(['date', 'time', 'code', 'close', 'open', 'volume'])
            for day, code in minute_keys:
                frame = _get_price(code, day + ' 09:30:00', day + ' 15:00:00', '1m')
                for stamp, row in frame.iterrows():
                    clock = str(stamp)[11:16]
                    if clock in TIMES:
                        w.writerow([day, clock, code, row['close'], row['open'] if clock == '09:31' else '', row['volume']])
        print('temperq_minutes.csv', len(minute_keys), '个成交日×标的')
    else:
        print('没有成交记录，跳过成交日分钟价')


if 'get_price' in globals():          # only inside a platform research environment
    main()
