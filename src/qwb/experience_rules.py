"""Safe daily rule cards and scorecards for user supplied investment experience.

The expression language is deliberately small. It evaluates only decision-day
information from a wide date x asset panel; Python code and arbitrary imports
are never executed.
"""
from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
import operator

import numpy as np
import pandas as pd

FIELDS = {"open", "high", "low", "close", "amount", "volume", "adj_factor"}
MAX_WINDOW = 504
MAX_CELLS = 10_000_000


class RuleSyntaxError(ValueError):
    pass


def _frame(panel, name):
    if name not in panel:
        raise RuleSyntaxError(f"数据缺少 {name}")
    value = panel[name]
    if not isinstance(value, pd.DataFrame):
        raise RuleSyntaxError(f"{name} 必须是日期 × 股票表")
    if (value.size > MAX_CELLS or not value.index.is_unique or not value.columns.is_unique
            or not value.index.is_monotonic_increasing):
        raise RuleSyntaxError("规则面板过大、日期无序或日期/证券重复")
    anchor = panel.get("close")
    if anchor is not None and (not value.index.equals(anchor.index) or not value.columns.equals(anchor.columns)):
        raise RuleSyntaxError("规则字段的日期和证券必须对齐")
    return value.astype(float)


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
        raise RuleSyntaxError(f"{name} 必须是有限数字")
    return float(value)


def evaluate(expression: str, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Evaluate the restricted expression without executing arbitrary code."""
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 300:
        raise RuleSyntaxError("规则表达式不能为空且不能超过 300 字")
    # ``return`` is a useful DSL word but a Python keyword; normalize only the
    # exact function token before parsing, while still using the whitelist below.
    expression = expression.replace("return(", "ret(")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise RuleSyntaxError("规则表达式语法无效") from exc
    def depth(node):
        return 1 + max((depth(child) for child in ast.iter_child_nodes(node)), default=0)
    if len(list(ast.walk(tree))) > 100 or depth(tree) > 20:
        raise RuleSyntaxError("规则表达式过于复杂")

    def walk(node):
        if isinstance(node, ast.Expression): return walk(node.body)
        if isinstance(node, ast.Name):
            if node.id not in FIELDS: raise RuleSyntaxError(f"不支持的数据字段：{node.id}")
            return _frame(panel, node.id)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return _number(node.value, "常量")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = walk(node.operand); return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and type(node.op) in {ast.Add, ast.Sub, ast.Mult, ast.Div}:
            left, right = walk(node.left), walk(node.right)
            fn = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                  ast.Div: operator.truediv}[type(node.op)]
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                return fn(left, right)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"mean", "std", "ret", "rank", "above", "below", "cross_up", "cross_down"}:
            name = node.func.id
            if node.keywords or not 1 <= len(node.args) <= 2: raise RuleSyntaxError(f"{name} 参数数量无效")
            value = walk(node.args[0])
            if name in {"mean", "std", "ret"} and len(node.args) != 2:
                raise RuleSyntaxError(f"{name} 需要字段和窗口")
            if name in {"above", "below", "cross_up", "cross_down"} and len(node.args) != 2:
                raise RuleSyntaxError(f"{name} 需要两个值")
            if name == "rank" and len(node.args) != 1:
                raise RuleSyntaxError("rank 仅接受一个参数")
            window = walk(node.args[1]) if name in {"mean", "std", "ret"} else None
            if window is not None:
                if type(window) not in (float, int) or not float(window).is_integer() or not 1 <= window <= MAX_WINDOW:
                    raise RuleSyntaxError(f"窗口必须为 1–{MAX_WINDOW} 的整数")
                window = int(window)
            if name in {"mean", "std"}:
                if not isinstance(value, pd.DataFrame) or window is None: raise RuleSyntaxError(f"{name} 需要字段和窗口")
                return getattr(value.rolling(window, min_periods=window), name)().replace([np.inf, -np.inf], np.nan)
            if name == "ret":
                if not isinstance(value, pd.DataFrame) or window is None: raise RuleSyntaxError("return 需要字段和窗口")
                return value.pct_change(window, fill_method=None).replace([np.inf, -np.inf], np.nan)
            if name == "rank":
                if not isinstance(value, pd.DataFrame): raise RuleSyntaxError("rank 需要一个字段或计算结果")
                return value.rank(axis=1, pct=True, method="average")
            if name in {"above", "below"}:
                if len(node.args) != 2: raise RuleSyntaxError(f"{name} 需要两个值")
                other = walk(node.args[1])
                comparison = value > other if name == "above" else value < other
                result = comparison.astype(float) if isinstance(comparison, pd.DataFrame) else float(comparison)
                valid = np.isfinite(value) & np.isfinite(other)
                return result.where(valid) if isinstance(result, pd.DataFrame) else (result if valid else np.nan)
            if name in {"cross_up", "cross_down"}:
                if len(node.args) != 2 or not isinstance(value, pd.DataFrame): raise RuleSyntaxError(f"{name} 需要两个序列表")
                other = walk(node.args[1])
                if not isinstance(other, pd.DataFrame): raise RuleSyntaxError(f"{name} 右侧必须是字段或均线")
                previous = value.shift(1) - other.shift(1)
                current = value - other
                result = ((previous <= 0) & (current > 0)).astype(float) if name == "cross_up" else ((previous >= 0) & (current < 0)).astype(float)
                return result.where(np.isfinite(previous) & np.isfinite(current))
        raise RuleSyntaxError("规则含有不支持的语法；只允许字段、算术和受限函数")

    result = walk(tree)
    if not isinstance(result, pd.DataFrame):
        result = pd.DataFrame(result, index=_frame(panel, "close").index, columns=_frame(panel, "close").columns)
    return result.replace([np.inf, -np.inf], np.nan)


@dataclass(frozen=True)
class RuleCard:
    id: str
    label: str
    expression: str
    weight: float = 1.0
    required: bool = False
    origin: str = "user"
    transform: str = "rank"

    def __post_init__(self):
        if not self.id.strip() or not self.label.strip(): raise ValueError("规则卡需要编号和名称")
        if isinstance(self.weight, bool) or not np.isfinite(self.weight) or self.weight == 0:
            raise ValueError("规则卡权重必须是非零有限数字")
        if type(self.required) is not bool or self.origin not in {"user", "template", "imported"}:
            raise ValueError("规则卡用途或来源无效")
        if self.transform not in {"rank", "binary"}:
            raise ValueError("规则卡转换无效")


@dataclass(frozen=True)
class Scorecard:
    id: str
    name: str
    rules: tuple[RuleCard, ...]
    universe_note: str = "由数据预检和用户授权决定"

    def __post_init__(self):
        if not self.id.strip() or not self.name.strip() or not 1 <= len(self.rules) <= 20:
            raise ValueError("打分卡需要名称和至少一条规则")
        if len({r.id for r in self.rules}) != len(self.rules): raise ValueError("规则卡编号重复")

    def evaluate(self, panel):
        scores = None; eligible = None; details = {}; validity = None
        for rule in self.rules:
            value = evaluate(rule.expression, panel)
            details[rule.id] = value
            valid = value.notna()
            validity = valid if validity is None else validity & valid
            normalized = value.rank(axis=1, pct=True, method="average") if rule.transform == "rank" else value
            scores = normalized * rule.weight if scores is None else scores + normalized * rule.weight
            if rule.required:
                mask = value > 0
                eligible = mask if eligible is None else eligible & mask
        if eligible is None: eligible = pd.DataFrame(True, index=scores.index, columns=scores.columns)
        scores = scores.where(validity)
        return {"scorecard_id": self.id, "name": self.name, "scores": scores,
                "eligible": eligible & validity, "valid": validity, "rule_values": details,
                "rules": [asdict(rule) for rule in self.rules],
                "scope": "historical_rule_score_not_performance_claim"}
