"""Material templates and append-only replacement of an imported file category."""
from __future__ import annotations

from . import strategy_imports as imports

MATERIALS = {
    'code': ('策略代码', '上传原策略 .py/.txt，或粘贴原文；代码只读不运行。'),
    'fills': ('回测交易', '每行一笔真实成交，费用填该笔已扣/分摊金额，不重复填整单费用。可补委托编号、成交编号、佣金计费单位和合同/交割单依据；未知请留空。按委托核验还须确认该订单最终全部成交已导出。'),
    'positions': ('每日持仓', '导出完整逐日持仓；BigQuant保留总资产和现金汇总行，聚宽保留Cash行。'),
    'snapshots': ('账户汇总', '每个交易日的总权益与现金，与持仓和成交保持同次回测。'),
    'logs': ('运行日志', '保留原始时间、级别和内容。普通日志不能替代完整订单生命周期。'),
    'orders': ('委托详情', '每笔委托的提交、成交与撤单；用于解释信号选中却没买到的原因。'),
    'minute_prices': ('成交日分钟价', '成交日每只成交标的的分钟价（date, time, code, close，可加 open、high、low、volume），用于检验成交时点敏感性和成交后最差价；只需成交日：对照时点加每笔成交后 10 分钟。'),
    'day_ranges': ('成交日全天区间', '每个成交日、每只成交标的的全天最高价和最低价（date, code, high, low，不复权），用于检验成交价是否专挑在当天低点买、高点卖。'),
    'prices': ('候选池收盘价', '候选池每只标的每个交易日的收盘价（date, code, close），用于检验策略前提；不只填买入的标的。'),
    'scores': ('每日打分', '覆盖每日候选池，不只填最终入选股票；available_at为当时实际可用时间。'),
    'quote_observations': ('日均报价价差', '仅用于量级核对，不是实际成交成本；measurement_scope填daily_quote_mean。'),
}

CHECK_MATERIALS = {
    'code': ('code',), 'syntax': ('code',), 'fills': ('fills',), 'fee_units': ('fills',),
    'holdings': ('positions', 'snapshots'), 'logs': ('logs',),
    'orders_vs_fills': ('logs',), 'scores': ('scores',),
    'period': ('code', 'fills', 'positions'), 'opening': ('positions', 'snapshots'),
    'negative_cash': ('positions', 'snapshots'), 'price_basis': ('code',),
    'inventory_bridge': ('positions', 'fills', 'logs'),
}


def template(kind, adapter='generic'):
    if kind not in MATERIALS or adapter not in {'auto', 'generic', 'bigquant', 'joinquant', 'ricequant'}:
        raise ValueError('材料模板不存在')
    if kind == 'code':
        return 'strategy.py', '# 请以你自己的完整策略代码替换本文件；不要直接上传空模板。\n# 保留初始资金、费用、回测区间、调仓设置及价格口径。\n'
    headers = {
        'fills': 'date,time,code,side,quantity,price,fee,order_id,fill_id,commission_basis,commission_evidence,order_complete',
        'positions': 'date,code,quantity,price',
        'snapshots': 'date,equity,cash',
        'logs': '时间,级别,内容',
        'scores': 'date,code,score,available_at',
        'minute_prices': 'date,time,code,close,open,high,low,volume',
        'day_ranges': 'date,code,high,low',
        'prices': 'date,code,close',
        'quote_observations': 'date,code,quoted_spread_bp,sample_count,measurement_scope',
    }
    if adapter == 'bigquant':
        headers.update(fills='日期,时间,证券代码,买/卖,数量,成交价,成交金额,交易费用,委托编号,成交编号,佣金计费单位,计费依据,委托成交完整',
                       positions='日期,证券代码,数量,持仓均价,收盘价,持仓市值')
    elif adapter == 'joinquant':
        headers['positions'] = '日期,品种,标的,多空,数量,可用数量,收盘价/结算价,市值/价值,总资产,仓位占比'
    return kind + '.csv', headers[kind] + '\n'


def replace_files(originals, previous, additions, kind):
    """Replace one declared category, keep every other original byte-for-byte."""
    if kind not in MATERIALS:
        raise ValueError('不支持的补充材料类型')
    if not additions:
        raise ValueError('请先选择要上传的文件')
    additions = imports.unpack_archives(additions)
    if any(material_kind(source) != kind for source in additions):
        raise ValueError('文件内容与所选材料不一致，请按该项模板填写或选择正确的上传项')
    if kind == 'code' and not any(line.strip() and not line.lstrip().startswith('#')
                                  for line in imports.decode_source(additions[0])[0].splitlines()):
        raise ValueError('请上传自己的策略代码，不能仅上传空模板或说明')
    kept = [source for source, info in zip(originals, previous['sources']) if info['kind'] != kind]
    files = kept + additions
    bundle = imports.normalize(files, name=previous['name'], opening_cash=previous['opening']['cash'])
    if any(s['rows'] == 0 for s in bundle['sources'][-len(additions):]):
        raise ValueError('模板尚未填写数据，请补齐后再上传')
    bundle['metadata'] = previous.get('metadata', {})
    return files, bundle


def replace_version(originals, previous, additions):
    """A re-run backtest: every uploaded kind replaces its old files; other kinds are kept."""
    if not additions:
        raise ValueError('请先选择优化版回测的导出文件')
    additions = imports.unpack_archives(additions)
    kinds = {material_kind(source) for source in additions}
    if 'fills' not in kinds and 'positions' not in kinds:
        raise ValueError('优化版至少需要新的回测交易或持仓文件')
    kept = [source for source, info in zip(originals, previous['sources']) if info['kind'] not in kinds]
    files = kept + additions
    bundle = imports.normalize(files, name=previous['name'], opening_cash=previous['opening']['cash'])
    if any(s['rows'] == 0 for s in bundle['sources'][-len(additions):]):
        raise ValueError('上传的文件没有数据行，请检查导出')
    bundle['metadata'] = {**previous.get('metadata', {}), 'replaced_kinds': sorted(kinds)}
    return files, bundle


def material_kind(source):
    if source.name.lower().endswith('.xlsx'):
        return 'platform_summary'
    if source.name.lower().endswith('.txt') and imports.is_platform_log(imports.decode_source(source)[0]):
        return 'logs'
    if source.name.lower().endswith('.xlsx'):
        return 'platform_summary'
    if source.name.lower().endswith('.json'):
        return 'engine_config'
    if source.name.lower().endswith(('.py', '.txt')):
        return 'code'
    from .strategy_import_formats import table
    header, _, _, _ = table(source)
    fields = set(header)
    for kind, required in (
        ('fills', {'买/卖', '成交价', '成交金额', '交易费用'}),
        ('fills', {'date', 'code', 'side', 'quantity', 'price', 'fee'}),
        ('positions', {'持仓均价', '收盘价', '持仓市值'}),
        ('positions', {'标的', '多空', '数量'}),
        ('positions', {'date', 'code', 'quantity'}),
        ('snapshots', {'date', 'equity', 'cash'}),
        ('logs', {'时间', '级别', '内容'}),
        ('platform_summary', {'指标', '数值'}),
        ('platform_summary', {'策略名'}),
        ('platform_returns', {'时间', '策略收益', '基准收益'}),
        ('fills', {'标的', '交易类型', '成交数量', '手续费'}),
        ('fills', {'合约代码', '买/卖', '成交量', '成交价'}),
        ('positions', {'合约代码', '仓位', '最新价'}),
        ('snapshots', {'账户类型', '可用资金', '总权益'}),
        ('scores', {'date', 'code', 'score'}),
        ('minute_prices', {'date', 'time', 'code', 'close'}),
        ('day_ranges', {'date', 'code', 'high', 'low'}),
        ('prices', {'date', 'code', 'close'}),
        ('scores', {'datetime', 'rank', 'order_book_id'}),
        ('orders', {'event', 'order_id', 'status', 'filled_quantity'}),
        ('fills', {'order_book_id', 'last_quantity', 'transaction_cost'}),
        ('positions', {'order_book_id', 'quantity', 'last_price', 'market_value'}),
        ('snapshots', {'cash', 'transaction_cost', 'market_value', 'total_value'}),
        ('platform_returns', {'date', 'cash', 'total_value', 'unit_net_value'}),
        ('platform_returns', {'日期', '策略净值', '基准净值'}),
        ('auxiliary', {'date', 'count', 'mean', 'std'}),
        ('scores', {'date', 'code', 'rank'}),
        ('quote_observations', {'date', 'code', 'quoted_spread_bp', 'sample_count', 'measurement_scope'}),
    ):
        if required <= fields:
            return kind
    raise ValueError('未识别材料表头，请使用对应模板或原平台导出')
