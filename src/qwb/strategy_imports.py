"""Versioned, read-only adapters for external strategy exports. Never execute code."""
from __future__ import annotations

import ast
import csv
from dataclasses import dataclass
from hashlib import sha256
import io
import math
import re

from .securities import security_code as stock_code
from .audit_fee_units import order_fields, validate_orders

SCHEMA = "strategy_export_v1"
# Trades, positions, account, code, log, scores, candidate prices, plus the extras a native report writes.
MAX_FILES = 10
MAX_FILE_BYTES = 8_000_000
MAX_TOTAL_BYTES = 24_000_000
MAX_ROWS = 50_000
MAX_CODE_BYTES = 200_000


@dataclass(frozen=True)
class SourceFile:
    name: str
    data: bytes


def numeric(value, label, *, summary=False, nonnegative=True):
    text = str(value).strip().replace(",", "")
    if summary:
        match = re.fullmatch(r"[^\d+\-]*([-+]?\d+(?:\.\d+)?)[^\d]*", text)
        if not match:
            raise ValueError(f"{label}：无法读取汇总金额")
        text = match[1]
    try:
        result = float(text)
    except ValueError:
        raise ValueError(f"{label}：应为数字") from None
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError(f"{label}：数字超出范围")
    return result


def date_value(value, label):
    from datetime import date
    text = value.strip()
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?", text)
    try:
        if match:  # Excel re-saves 2023-01-03 as 2023/1/3
            return date(int(match[1]), int(match[2]), int(match[3])).isoformat()
        return date.fromisoformat(text[:10]).isoformat() if re.fullmatch(r"\d{4}-\d{2}-\d{2}([ T].*)?", text) else date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError(f"{label}：日期应为 YYYY-MM-DD，读到“{text[:20]}”") from None


def _rows(text, name):
    reader = csv.DictReader(io.StringIO(text), strict=True)
    fields = reader.fieldnames or []
    if not fields or len(fields) != len(set(fields)):
        raise ValueError(f"{name}：表头为空或重复")
    rows = []
    for index, row in enumerate(reader, 2):
        if index > MAX_ROWS + 1:
            raise ValueError(f"{name}：最多 {MAX_ROWS} 行")
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f"{name} 第 {index} 行：列数不一致")
        rows.append((index, row))
    return set(fields), rows


def _ref(kind, row):
    return {"source": kind, "row": row}


def inspect_code(source):
    """Literal candidates only, with line provenance; these are not active rules."""
    if len(source.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError("策略代码不能超过 200KB")
    from .code_parse import parse
    tree, skipped = parse(source)
    if tree is None:
        py2 = re.search(r"^\s*print\s+[^(\s=]", source, re.M)
        return {"status": "unparsed", "candidates": [],
                "note": ("代码像是 Python 2 写法（print 不带括号），无法静态解析；成交与持仓仍可分析" if py2
                         else "代码无法静态解析；成交与持仓仍可分析")}
    keys = {"stock_count", "max_cash_per_instrument", "hold_days", "capital_base",
            "rebalance_period_days", "order_price_field_buy", "order_price_field_sell",
            "buy_cost", "sell_cost", "min_cost", "expr", "expr_filters", "mode",
            "start_date", "end_date", "start", "end", "frequency", "rebalance_period_type",
            "open_commission", "close_commission", "min_commission", "close_tax", "open_tax",
            "use_real_price", "avoid_future_data", "hold_count", "hold_num", "n_hold"}
    aliases = {"target_num", "stock_num", "buy_num", "max_hold", "etf_num"}
    candidates = []

    def add(key, value, line):
        alias = key if key in aliases else None
        if key not in keys and not alias:
            return
        try:
            literal = ast.literal_eval(value)
        except (ValueError, TypeError, RecursionError, MemoryError):
            return
        if isinstance(literal, (str, int, float)) and (not isinstance(literal, bool) or key in {'use_real_price', 'avoid_future_data'}):
            if isinstance(literal, float) and not math.isfinite(literal):
                return
            candidates.append({"key": key, "value": literal, "line": line, "scope": "unconfirmed_literal",
                               **({"alias": alias} if alias else {})})

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                add(getattr(target, "attr", getattr(target, "id", "")), node.value, node.lineno)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                add(keyword.arg, keyword.value, node.lineno)
            if getattr(node.func, 'id', '') == 'set_option' and len(node.args) == 2:
                try:
                    key = ast.literal_eval(node.args[0])
                except (ValueError, TypeError):
                    continue
                add(key, node.args[1], node.lineno)
    embedded = False
    for node in ast.walk(tree):
        # Local RQAlpha bridges keep the original strategy as a SOURCE string and the run settings in __config__.
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        if name.upper() == "SOURCE" and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            inner = inspect_code(node.value.value) if "\n" in node.value.value else {}
            for c in inner.get("candidates", []):
                candidates.append({**c, "embedded": True})
            embedded = embedded or bool(inner.get("candidates"))
        elif name == "__config__":
            try:
                config = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError, RecursionError, MemoryError):
                continue
            base = config.get("base", {}) if isinstance(config, dict) else {}
            accounts = base.get("accounts") if isinstance(base.get("accounts"), dict) else {}
            for key, value in (("start_date", base.get("start_date")), ("end_date", base.get("end_date")),
                               ("capital_base", accounts.get("stock"))):
                if isinstance(value, (int, float, str)) and value != "":
                    candidates.append({"key": key, "value": value, "line": node.lineno,
                                       "scope": "unconfirmed_literal", "config": True})
    candidates.extend(_alias_candidates(candidates))
    return {"status": "parsed", "candidates": candidates, "skipped_header_lines": skipped,
            "note": "静态字面量候选，可能位于未调用函数或分支；未执行代码，未激活规则"
                    + (f"；已跳过开头 {skipped} 行说明文字" if skipped else "")
                    + ("；含代码内嵌的原策略与回测配置" if embedded else "")}


def _alias_candidates(candidates):
    """Common JoinQuant names for the holding count (target_num, stock_num) as hold_num."""
    have = {c["key"] for c in candidates}
    if "hold_num" in have:
        return []
    return [{**c, "key": "hold_num", "alias_of": c["alias"]} for c in candidates if c.get("alias")]


MAX_UNPACKED_BYTES = 40_000_000


def unpack_archives(files):
    """JoinQuant downloads each export as a zip holding one CSV or TXT; open those in place.

    Archives are read in memory, one level deep, only for csv/txt/py members, with the
    uncompressed size checked before reading so a crafted archive cannot exhaust memory.
    """
    import zipfile
    out = []
    for source in files:
        if not source.name.lower().endswith('.zip'):
            out.append(source)
            continue
        try:
            archive = zipfile.ZipFile(io.BytesIO(source.data))
        except zipfile.BadZipFile:
            raise ValueError(f"{source.name}：压缩包损坏，请重新下载") from None
        members = [m for m in archive.infolist() if not m.is_dir()
                   and not m.filename.startswith(('__MACOSX/', '.')) and '/._' not in m.filename]
        if not members or len(members) > 3:
            raise ValueError(f"{source.name}：压缩包应只含一个导出文件")
        if sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
            raise ValueError(f"{source.name}：解压后超过 40MB")
        for member in members:
            inner = member.filename.rsplit('/', 1)[-1]
            if not inner.lower().endswith(('.csv', '.txt', '.py')):
                raise ValueError(f"{source.name}：压缩包里只能是 CSV 或 TXT")
            out.append(SourceFile(inner, archive.read(member)))
    return out


def is_platform_log(text):
    """JoinQuant: 'YYYY-MM-DD HH:MM:SS - LEVEL - message'. RQAlpha: '[YYYY-MM-DD HH:MM:SS.ffffff] LEVEL: message',
    possibly several logs joined under '===== name.log =====' headers, with warnings and tracebacks between."""
    head = [line for line in text.splitlines()[:20] if line.strip()]
    matched = sum(bool(LOG_LINE.match(line)) for line in head)
    if len(head) >= 3 and bool(LOG_LINE.match(head[0])) and matched >= .6 * len(head):
        return True
    lines = text.splitlines()[:400]
    sections = sum(bool(LOG_SECTION.match(line)) for line in lines)
    rq = sum(bool(RQ_LOG_LINE.match(line)) for line in lines)
    # Strategy code never starts with a log section header, and has no bracketed timestamps at line start.
    return bool(lines) and (bool(LOG_SECTION.match(lines[0])) or rq >= 3) and (sections or rq >= .3 * len([l for l in lines if l.strip()]))


def parse_log(text):
    """Rows {时间, 级别, 内容, line} from either format; continuation lines join the previous message.
    RQAlpha console.log and engine.log repeat the same lines; each (time, level, message) is kept once."""
    rows, seen = [], set()
    for index, line in enumerate(text.splitlines(), 1):
        if LOG_SECTION.match(line):
            continue
        match = LOG_LINE.match(line) or RQ_LOG_LINE.match(line)
        if match:
            key = (match[1], match[2], match[3].strip())
            if key in seen:
                rows.append(None)                 # a duplicate swallows its own continuation lines
                continue
            seen.add(key)
            rows.append({'时间': match[1], '级别': match[2], '内容': match[3].strip(), 'line': index})
        elif rows and rows[-1] is not None and line.strip():
            rows[-1]['内容'] += chr(10) + line
    return [r for r in rows if r is not None]


LOG_LINE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - ([A-Z]+)\s*- (.*)")
RQ_LOG_LINE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\] ([A-Z]+): (.*)")
LOG_SECTION = re.compile(r"=====\s*[\w.\-]+\.log\s*=====\s*$")


def decode_source(source):
    """Decode without dropping bytes; retain the identified encoding in provenance."""
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            text = source.data.decode(encoding)
            if '\x00' in text:
                raise ValueError('文件含空字符，请导出 CSV 或 Python 文本')
            return text, encoding
        except UnicodeDecodeError:
            continue
    raise ValueError('文件编码无法识别，请使用 UTF-8 或 GBK 文本')


def bigquant(files, *, name, opening_cash=None):
    """Normalize merged date cells before sorting; retain row-level provenance."""
    result = {"schema_version": SCHEMA, "adapter": "bigquant_v1", "name": name,
              "opening": {"kind": "empty" if opening_cash is not None else "first_close",
                          "cash": opening_cash},
              "fills": [], "positions": [], "snapshots": [], "actions": [],
              "logs": [], "external_attribution": [], "quote_observations": [], "scores": [], "prices": [], "code": {}, "sources": []}
    seen = set()
    for source in files:
        text, encoding = decode_source(source)
        if source.name.lower().endswith((".py", ".txt")):
            kind = "code"
            result["code"] = inspect_code(text)
            result['code']['source'] = text
            row_count = len(text.splitlines())
        else:
            columns, rows = _rows(text, source.name)
            if {"买/卖", "成交价", "成交金额", "交易费用"} <= columns:
                kind = "fills"
                required = {"日期", "证券代码", "数量", "时间"}
            elif {"持仓均价", "收盘价", "持仓市值"} <= columns:
                kind = "positions"
                required = {"日期", "证券代码", "数量"}
            elif {"时间", "级别", "内容"} <= columns:
                kind = "logs"; required = set()
            elif {"行业名称", "组合收益率", "基准收益率"} <= columns:
                kind = "external_attribution"; required = set()
            elif {"date", "code", "quoted_spread_bp", "sample_count", "measurement_scope"} <= columns:
                kind = "quote_observations"; required = set()
            elif {'date', 'code', 'score'} <= columns:
                kind = 'scores'; required = set()
            elif {'date', 'code', 'close'} <= columns:
                kind = 'prices'; required = set()
            else:
                raise ValueError(f"{source.name}：未识别的 BigQuant 表头")
            if required - columns:
                raise ValueError(f"{source.name}：缺少列 {','.join(sorted(required - columns))}")
            prior_date = ""
            for index, row in rows:
                ref = _ref(kind, index)
                label = f"{source.name} 第 {index} 行"
                if kind in {"fills", "positions"}:
                    prior_date = row["日期"].strip() or prior_date
                    day = date_value(prior_date, label)
                    code = row["证券代码"].strip()
                if kind == "fills":
                    side = row["买/卖"].strip()
                    if not side and not code:
                        continue
                    if side not in {"买入", "卖出"} or not code:
                        raise ValueError(label + "：成交方向或证券代码缺失")
                    quantity = numeric(row["数量"], label)
                    price = numeric(row["成交价"], label)
                    if quantity <= 0 or not quantity.is_integer() or price <= 0:
                        raise ValueError(label + "：成交股数应为正整数，价格应大于零")
                    result["fills"].append({"date": day, "time": row["时间"], "code": stock_code(code),
                        "side": "buy" if side == "买入" else "sell", "quantity": int(quantity),
                        "price": price, "notional": numeric(row["成交金额"], label),
                        "fee": numeric(row["交易费用"], label), "ref": ref, **order_fields(row)})
                elif kind == "positions":
                    if not code:
                        result["snapshots"].append({"date": day,
                            "equity": numeric(row["持仓均价"], label, summary=True),
                            "cash": numeric(row["收盘价"], label, summary=True),
                            "market_value": numeric(row["持仓市值"], label, summary=True), "ref": ref})
                    elif code != "--":
                        quantity = numeric(row["数量"], label)
                        if quantity <= 0 or not quantity.is_integer():
                            raise ValueError(label + "：持仓股数应为正整数")
                        result["positions"].append({"date": day, "code": stock_code(code),
                            "quantity": int(quantity), "price": numeric(row["收盘价"], label),
                            "market_value": numeric(row["持仓市值"], label), "ref": ref})
                elif kind == "logs":
                    result["logs"].append({**row, "ref": ref})
                    match = re.search(r"split\[(\d{4}-\d{2}-\d{2})\]\[([^,]+),old=(\d+),now=(\d+),cash=([-\d.]+)\]", row["内容"])
                    if match:
                        day, code, old, new, cash = match.groups()
                        result["actions"].append({"date": date_value(day, label), "code": stock_code(code),
                            "quantity_change": int(new) - int(old),
                            "cash": numeric(cash, label, nonnegative=False), "ref": ref})
                elif kind == "quote_observations":
                    spread, count = (numeric(row[k], label) for k in ("quoted_spread_bp", "sample_count"))
                    if spread <= 0 or spread > 10000 or count < 1 or not count.is_integer():
                        raise ValueError(label + "：报价价差须在(0,10000]bp内，快照数须为正整数")
                    if row["measurement_scope"] != "daily_quote_mean":
                        raise ValueError(label + "：本版仅接受 daily_quote_mean 日均报价价差核对")
                    result["quote_observations"].append({"date": date_value(row["date"], label),
                        "code": stock_code(row["code"]), "quoted_spread_bp": spread, "sample_count": int(count),
                        "measurement_scope": row["measurement_scope"], "ref": ref})
                elif kind == 'prices':
                    result['prices'].append({'date': date_value(row['date'], label),
                        'code': stock_code(row['code'].replace('.XSHG', '.SH').replace('.XSHE', '.SZ')),
                        'close': numeric(row['close'], label), 'ref': ref})
                elif kind == 'scores':
                    result['scores'].append({'date': date_value(row['date'], label),
                        'code': stock_code(row['code'].replace('.XSHG', '.SH').replace('.XSHE', '.SZ')),
                        'score': numeric(row['score'], label, nonnegative=False),
                        'available_at': row.get('available_at', '').strip(), 'ref': ref})
                else:
                    result["external_attribution"].append({**row, "ref": ref})
            row_count = len(rows)
        if kind in seen:
            raise ValueError("每种材料只接收一个文件，请先合并同类导出")
        seen.add(kind)
        result["sources"].append({"kind": kind, "name": source.name, "sha256": sha256(source.data).hexdigest(),
                                  "bytes": len(source.data), "rows": row_count, 'encoding': encoding})
    if not seen & {'fills', 'positions', 'code'}:
        raise ValueError('请上传策略代码或回测交易、持仓材料')
    for key, identity in (("snapshots", lambda r: r["date"]),
                          ("positions", lambda r: (r["date"], r["code"])),
                          ("quote_observations", lambda r: (r["date"], r["code"]))):
        identities = [identity(row) for row in result[key]]
        if len(identities) != len(set(identities)):
            raise ValueError("持仓、每日汇总或报价观测重复，请使用一致口径的导出")
    dates = {row["date"] for row in result["snapshots"]}
    if dates and any(row["date"] not in dates for key in ("fills", "positions", "actions") for row in result[key]):
        raise ValueError("成交、公司行动或持仓日期缺少对应的每日账户汇总")
    for key in ("fills", "positions", "snapshots", "actions", "quote_observations", 'scores'):
        result[key].sort(key=lambda row: row["date"])
    return result


ADAPTERS = {"bigquant": bigquant}


def normalize(files, *, adapter="auto", name="导入策略", opening_cash=None):
    if adapter not in {'auto', 'bigquant', 'joinquant', 'ricequant', 'generic'}:
        raise ValueError("暂不支持该平台适配器")
    files = unpack_archives(files)
    if not 1 <= len(files) <= MAX_FILES:  # the web parser admits two more so this message, not a bare 400, explains it
        raise ValueError(f"每次最多上传 {MAX_FILES} 个文件（ZIP 按解压后的文件计）")
    if not name.strip() or len(name) > 80:
        raise ValueError("策略名称应为 1–80 个字符")
    if opening_cash is not None:
        opening_cash = numeric(opening_cash, "期初现金")
        if opening_cash <= 0:
            raise ValueError("期初现金应大于零")
    total = 0
    for source in files:
        if not source.name or len(source.name) > 120 or any(c in source.name for c in '/\\:\x00'):
            raise ValueError("文件名无效，请只上传文件本身")
        if not source.name.lower().endswith((".csv", ".py", ".txt")):
            raise ValueError("只支持 CSV、TXT/PY，或平台下载的 ZIP")
        if len(source.data) > MAX_FILE_BYTES:
            raise ValueError(f"{source.name}：单个文件不能超过 8MB")
        total += len(source.data)
    if total > MAX_UNPACKED_BYTES:
        raise ValueError("文件解压后合计不能超过 40MB")
    try:
        from .strategy_import_formats import detect_adapter, other_platform
        detected = detect_adapter(files)
        chosen = detected if adapter == 'auto' else adapter
        if detected not in {'code_only', chosen}:
            raise ValueError('所选来源平台与文件表头不一致，请选择自动识别')
        if chosen in {'joinquant', 'ricequant', 'generic'}:
            result = other_platform(files, adapter=chosen, name=name.strip(), opening_cash=opening_cash)
        else:
            result = bigquant(files, name=name.strip(), opening_cash=opening_cash)
        if adapter == 'auto' and detected == 'code_only':
            result['adapter'] = 'code_only_v1'
        validate_orders(result['fills'])
        if result['adapter'] in {'joinquant_v1', 'ricequant_v1', 'generic_v1'}:
            # These exports omit dividend and bonus-share records; recover the ones the account pins down.
            from . import corporate_actions
            result = corporate_actions.apply(result, corporate_actions.infer(result))
        return result
    except csv.Error:
        raise ValueError("CSV 格式损坏或单元格过长，请重新导出") from None
