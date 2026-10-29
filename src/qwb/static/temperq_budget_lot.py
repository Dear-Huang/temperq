# -*- coding: utf-8 -*-
"""最大可买整手数：替代"下单金额乘 0.99"这类经验余量。粘贴进策略，买入前调用。

    q = temperq_max_lot(cash, price, slippage=0.001, rate=0.0003, minimum=5, lot=100)
    order(code, q)

q 满足 q*(price+slippage) + max(rate*q*(price+slippage), minimum) <= cash，且再多一手就超过。
这是按你声明的费用和滑点算的；实盘成交价不同时仍可能不足，不是成交保证。
"""


def temperq_max_lot(cash, price, slippage=0.0, rate=0.0003, minimum=5.0, lot=100):
    unit = price + slippage
    if cash <= 0 or unit <= 0:
        return 0
    cost = lambda q: 0.0 if q == 0 else q * unit + max(rate * q * unit, minimum)
    q = int(cash / (unit * (1 + rate)) // lot) * lot      # start near the answer, then step to the exact edge
    while q > 0 and cost(q) > cash:
        q -= lot
    while cost(q + lot) <= cash:
        q += lot
    return q
