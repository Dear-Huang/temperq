"""Versioned, round-trippable rule data shared by the editor and local tools."""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy

from .experience_rules import RuleCard, Scorecard, MAX_WINDOW

SCHEMA = "rule_card_v1"
MAX_BYTES = 64_000
FIELDS = {"close": "收盘价", "open": "开盘价", "high": "最高价", "low": "最低价",
          "amount": "成交额", "volume": "成交量"}
OPERATORS = {"above_mean": "高于均值", "mean_above_mean": "短均线高于长均线",
             "return": "区间涨幅", "low_volatility": "低波动",
             "return_above": "区间涨幅超过", "above_mean_ratio": "高于均值倍数"}
PRICE_FIELDS = {"close", "open", "high", "low"}
KEYS = {"id", "field", "operator", "window", "value", "weight", "required", "origin", "description"}
ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def number(value, name, low, high, *, integer=False):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{name} 须在 {low}–{high} 之间")
    if integer and (type(value) is not int):
        raise ValueError(f"{name} 须为整数")
    return value


def expression(rule):
    field, op, n, value = (rule[k] for k in ("field", "operator", "window", "value"))
    formulas = {
        "above_mean": f"above({field}, mean({field}, {n}))",
        "mean_above_mean": f"above(mean({field}, {n}), mean({field}, {value}))",
        "return": f"ret({field}, {n})",
        "low_volatility": f"-std(ret({field}, 1), {n})",
        "return_above": f"above(ret({field}, {n}), {value})",
        "above_mean_ratio": f"above({field}, mean({field}, {n}) * {value})",
    }
    return formulas[op]


def description(rule):
    field, op, n, value = (rule[k] for k in ("field", "operator", "window", "value"))
    label = FIELDS[field]
    if op == "above_mean": return f"{label}高于 {n} 日均值"
    if op == "mean_above_mean": return f"{label} {n} 日均线高于 {value} 日均线"
    if op == "return": return f"{label}近 {n} 日涨幅（{'越低' if rule['weight'] < 0 else '越高'}加分）"
    if op == "low_volatility": return f"{label} {n} 日收益波动{'越低' if rule['weight'] > 0 else '越高'}加分"
    if op == "return_above": return f"{label}近 {n} 日涨幅超过 {value:.1%}"
    return f"{label}高于 {n} 日均值的 {value:g} 倍"


def validate(document, *, imported=False):
    if not isinstance(document, dict) or set(document) != {"schema", "id", "name", "rules"}:
        raise ValueError("规则文件只能包含 schema、id、name、rules")
    result = deepcopy(document)
    if result["schema"] != SCHEMA: raise ValueError("不支持的规则卡格式版本")
    if not isinstance(result["id"], str) or not ID.fullmatch(result["id"]): raise ValueError("规则卡编号无效")
    if (not isinstance(result["name"], str) or not result["name"].strip() or len(result["name"]) > 80
            or any(c in result["name"] for c in "\r\n\x00")):
        raise ValueError("规则卡名称须为 1–80 字的单行文字")
    if not isinstance(result["rules"], list) or not 1 <= len(result["rules"]) <= 20:
        raise ValueError("每份规则卡须有 1–20 条规则")
    seen = set()
    for i, rule in enumerate(result["rules"], 1):
        try:
            if not isinstance(rule, dict) or set(rule) - KEYS or KEYS - {"description"} - set(rule):
                raise ValueError("规则字段无效或缺失")
            if not isinstance(rule["id"], str) or not ID.fullmatch(rule["id"]) or rule["id"] in seen:
                raise ValueError("编号无效或重复")
            seen.add(rule["id"])
            if rule["field"] not in FIELDS or rule["operator"] not in OPERATORS:
                raise ValueError("字段或操作符不在白名单中")
            number(rule["window"], "窗口", 1, MAX_WINDOW, integer=True)
            number(rule["value"], "数值", -100, MAX_WINDOW)
            number(rule["weight"], "权重", -10, 10)
            if rule["weight"] == 0 or type(rule["required"]) is not bool:
                raise ValueError("权重不能为零，用途须为布尔值")
            if rule["origin"] not in {"template", "user", "imported"}: raise ValueError("来源无效")
            op = rule["operator"]
            if op in {"return", "low_volatility", "return_above", "mean_above_mean"} and rule["field"] not in PRICE_FIELDS:
                raise ValueError("此操作只支持价格字段")
            if op == "mean_above_mean":
                number(rule["value"], "长均线窗口", rule["window"] + 1, MAX_WINDOW, integer=True)
            elif op == "above_mean_ratio":
                number(rule["value"], "均值倍数", .1, 10)
            elif op == "return_above": number(rule["value"], "涨幅阈值", -.99, 10)
            elif rule["value"] != 0: raise ValueError("此操作的附加数值必须为 0")
            if op == "low_volatility" and rule["window"] < 2: raise ValueError("波动窗口至少为 2")
            if rule["required"] and op not in {"above_mean", "mean_above_mean", "return_above", "above_mean_ratio"}:
                raise ValueError("必须满足仅用于条件规则")
            expected = description(rule)
            if "description" in rule and rule["description"] != expected:
                raise ValueError("中文复述与实际规则不一致")
            rule["description"] = expected
            if imported: rule["origin"] = "imported"
        except (ValueError, TypeError) as exc:
            raise ValueError(f"第 {i} 条规则：{exc}") from exc
    return result


def loads(raw, *, imported=False):
    if len(raw) > MAX_BYTES: raise ValueError("规则文件超过 64 KB")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError(f"JSON 字段重复：{key}")
            result[key] = value
        return result
    try:
        return validate(json.loads(raw, object_pairs_hook=pairs), imported=imported)
    except (UnicodeError, RecursionError, TypeError) as exc:
        raise ValueError("规则文件不是有效 JSON") from exc


def dumps(document):
    return json.dumps(validate(document), ensure_ascii=False, indent=2, allow_nan=False)


def scorecard(document):
    doc = validate(document)
    return Scorecard(doc["id"], doc["name"], tuple(
        RuleCard(r["id"], r["description"], expression(r), r["weight"], r["required"], r["origin"],
                 "rank" if r["operator"] in {"return", "low_volatility"} else "binary") for r in doc["rules"]))


def report(document):
    doc = validate(document)
    return {"valid": True, "schema": SCHEMA, "errors": [], "name": doc["name"],
            "required_fields": sorted({r["field"] for r in doc["rules"]} | {"adj_factor"}),
            "rules": [{"id": r["id"], "description": r["description"], "origin": r["origin"],
                       "required": r["required"], "weight": r["weight"]} for r in doc["rules"]]}


def template(template_id):
    def rule(id, field, operator, window, value=0, weight=1, required=False):
        return dict(id=id, field=field, operator=operator, window=window, value=value,
                    weight=weight, required=required, origin="template")
    templates = {
        "trend_following_daily": ("趋势跟随", [rule("close_above_ma20", "close", "above_mean", 20, required=True),
            rule("ma20_above_ma60", "close", "mean_above_mean", 20, 60), rule("momentum_60", "close", "return", 60)]),
        "short_reversal_daily": ("短期反转", [rule("reversal", "close", "return", 5, weight=-1),
            rule("price_above", "close", "above_mean", 20, required=True)]),
        "low_volatility_daily": ("低波稳健", [rule("low_vol", "close", "low_volatility", 20),
            rule("liquidity", "amount", "above_mean", 20)]),
        "volume_price_daily": ("量价配合", [rule("positive", "close", "return_above", 5),
            rule("amount_expand", "amount", "above_mean_ratio", 20, 1)]),
    }
    if template_id not in templates: raise ValueError("未知经验规则模板")
    name, rules = templates[template_id]
    return validate(dict(schema=SCHEMA, id=template_id, name=name, rules=rules))


def catalog():
    return [template(key) for key in ("trend_following_daily", "short_reversal_daily",
                                    "low_volatility_daily", "volume_price_daily")]
