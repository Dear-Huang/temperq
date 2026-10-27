"""Small executable starting points for users who bring investment experience."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from . import policy


@dataclass(frozen=True)
class Playbook:
    id: str
    name: str
    promise: str
    audience: str
    signal_source: str
    rule_template: str
    preset: str
    overrides: dict
    first_question: str
    next_step: str


PLAYBOOKS = {
    "first_strategy": Playbook("first_strategy", "先跑起来", "用一套能解释的默认规则开出第一版账户。", "第一次使用", "public40_lgbm_v1", "trend_following_daily", "direct", {}, "你愿意先接受一套基线，再逐项替换自己的想法吗？", "下载行情 → 运行基线 → 再填自己的经验"),
    "patient_owner": Playbook("patient_owner", "长期持有", "允许排名有小幅变化，减少无意义的来回交易。", "有持仓、重视纪律", "public40_lgbm_v1", "low_volatility_daily", "patient", {}, "哪些股票即使排名下降，你仍然愿意继续持有？", "导入持仓 → 设定保留带 → 看成本与受阻交易"),
    "active_rebalancer": Playbook("active_rebalancer", "规则换仓", "按固定周期执行清晰的换仓规则，并把经验变成可复查版本。", "有明确买卖纪律", "public40_lgbm_v1", "volume_price_daily", "direct", {}, "你多久检查一次信号，什么情况才允许临时调整？", "选择周期 → 设置交易预算 → 开影子账户"),
}


def catalog():
    return {key: asdict(value) for key, value in PLAYBOOKS.items()}


def get(playbook_id: str):
    try:
        return PLAYBOOKS[playbook_id]
    except KeyError as exc:
        raise ValueError("未知经验策略模板") from exc


def resolve(playbook_id: str, *, capital=None, overrides=None):
    value = get(playbook_id)
    resolved = policy.resolve(value.preset, {**value.overrides, **(overrides or {})}, capital=capital)
    resolved["playbook"] = {"id": value.id, "name": value.name, "signal_source": value.signal_source, "rule_template": value.rule_template, "scope": "starting_policy_not_a_performance_claim"}
    return resolved
