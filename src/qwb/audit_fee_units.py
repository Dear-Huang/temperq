"""Explicit charging units, independent of any external trading engine."""
from __future__ import annotations

from collections import defaultdict
import math


def order_fields(row):
    """Optional CSV contract. An order identifier alone never proves a fee basis."""
    aliases = {'order_id': '委托编号', 'fill_id': '成交编号',
               'commission_basis': '佣金计费单位', 'commission_evidence': '计费依据',
               'order_complete': '委托成交完整'}
    out = {}
    for key, alias in aliases.items():
        value = str(row.get(key, row.get(alias, ''))).strip()
        if len(value) > 240:
            raise ValueError('订单标识或计费依据超过240字符')
        out[key] = value
    out['commission_basis'] = {'按委托': 'order', '按成交': 'fill', '未知': 'unknown', '': 'unknown'}.get(
        out['commission_basis'], out['commission_basis'])
    if out['commission_basis'] not in {'order', 'fill', 'unknown'}:
        raise ValueError('佣金计费单位须为 order/fill/unknown')
    complete = out['order_complete'].lower()
    if complete not in {'', 'true', 'false', '1', '0', '是', '否'}:
        raise ValueError('委托成交完整须为 true/false；仅全部最终成交已导出时填true')
    out['order_complete'] = complete in {'true', '1', '是'}
    if out['commission_basis'] != 'unknown' and not out['commission_evidence']:
        raise ValueError('确认佣金计费单位时须填写计费依据（合同或交割单引用）')
    if out['commission_basis'] == 'order' and not out['order_id']:
        raise ValueError('按委托计费须提供委托编号')
    return out


def validate_orders(fills):
    seen, orders = set(), {}
    for fill in fills:
        if not math.isfinite(fill['notional']) or fill['notional'] <= 0:
            raise ValueError('真实成交或净变动的参考金额须为有限正数')
        day, order = fill['date'], fill.get('order_id')
        if fill.get('fill_id'):
            key = (day, fill['fill_id'])
            if key in seen:
                raise ValueError('同日成交编号重复，请勿重复导入部分成交')
            seen.add(key)
        if not order:
            continue
        signature = (fill['code'], fill['side'], fill.get('commission_basis', 'unknown'),
                     fill.get('commission_evidence', ''), fill.get('order_complete', False))
        key = (day, order)
        if key in orders and orders[key] != signature:
            raise ValueError('同日同委托编号的证券、方向、计费依据或完整性不一致')
        orders[key] = signature


def charging_units(fills, fees):
    """One minimum per declared unit; unknown rows remain labeled scenarios."""
    validate_orders(fills)
    grouped = defaultdict(list)
    for index, fill in enumerate(fills):
        basis = fill.get('commission_basis', 'unknown')
        order = fill.get('order_id')
        key = (fill['date'], 'order', order) if order and basis != 'fill' else (fill['date'], 'fill', index)
        grouped[key].append(index)
    units = []
    for key, indexes in grouped.items():
        members = [fills[i] for i in indexes]
        first = members[0]
        value = sum(f['notional'] for f in members)
        basis = first.get('commission_basis', 'unknown')
        confirmed = bool(first.get('commission_evidence')) and (
            basis == 'fill' or (basis == 'order' and first.get('order_complete') is True))
        commission = max(fees.minimum_commission, value * fees.commission_bp / 10000)
        units.append({'date': first['date'], 'code': first['code'], 'side': first['side'],
            'order_id': first.get('order_id', ''), 'basis': key[1], 'declared_basis': basis,
            'confirmed': confirmed, 'evidence': first.get('commission_evidence', ''),
            'indexes': indexes, 'fill_count': len(indexes), 'notional': value,
            'modeled_commission': commission,
            'minimum_premium': max(0., commission - value * fees.commission_bp / 10000),
            'per_fill_commission_scenario': sum(max(fees.minimum_commission, f['notional'] * fees.commission_bp / 10000) for f in members),
            'refs': [f['ref'] for f in members]})
    return units
