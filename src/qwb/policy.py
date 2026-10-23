"""Versioned presets and a schema adapter over the authoritative governance config."""
from __future__ import annotations

from dataclasses import asdict, fields
from math import ceil
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .governance import GovernanceConfig
from . import scenario

DEFAULTS_VERSION = "policy_defaults_v1"
PRESETS = {
    "direct": {"name": "直接调整", "description": "每五日向目标调整；不预测风险，不放慢建仓。",
               "mode": "fixed", "overrides": {}},
    "patient": {"name": "分步靠近目标", "description": "每次调整一半差距；退出选股范围和硬限制优先处理。",
                "mode": "fixed", "overrides": {"adjustment_speed": .5}},
    "risk": {"name": "波动目标", "description": "用 EWMA 估计近期风险并缩放仓位；不是回撤保证。",
             "mode": "ewma", "overrides": {}},
}
HELP = {
    "n_hold": ("持仓只数", "只", "决定目标分散到多少只股票"),
    "hold_until_rank": ("继续持有的排名上限", "名", "仍在这个排名以内就保留，减少反复更换"),
    "max_weight": ("单票目标上限", "比例", "超出部分留现金，不放大其他股票"),
    "exposure_cap": ("股票总仓位上限", "比例", "目标仓位最多占权益多少"),
    "cash_floor": ("最低现金", "比例", "目标中至少留多少现金"),
    "target_volatility": ("目标年化风险", "比例", "仅在 EWMA / ML 风险控制下生效，不是亏损上限"),
    "adjustment_speed": ("每期靠近目标的比例", "比例", "1 为立即调整；0.5 为本期走一半差距，硬限制优先"),
    "commission_bp": ("单边佣金", "bp", "按自己的券商填写，默认只是工程假设"),
    "minimum_commission": ("每笔最低佣金", "元", "可填写实际券商口径"),
    "slippage_bp": ("单边滑点压力", "bp", "人为成本压力，不能当成已经标定的价差/冲击"),
}


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)
    mode: Literal["fixed", "ewma", "ml"] = "fixed"
    parameters: GovernanceConfig = Field(default_factory=GovernanceConfig)

    @model_validator(mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value):
        if isinstance(value, dict) and isinstance(value.get("parameters"), dict):
            if any(isinstance(v, bool) for v in value["parameters"].values()):
                raise ValueError("数值参数不能使用布尔值")
            known = {f.name for f in fields(GovernanceConfig)}
            if set(value["parameters"]) - known:
                raise ValueError("存在不支持的政策参数")
        return value


def resolve(preset, overrides=None, mode=None, *, capital=None):
    if preset not in PRESETS:
        raise ValueError("请选择已有政策预设")
    base = asdict(GovernanceConfig())
    values = dict(base, **PRESETS[preset]["overrides"])
    derived={}
    if capital is not None:
        tier=scenario.for_equity(capital["equity"])
        n=tier["n_hold"]
        derived={"n_hold":n,"hold_until_rank":ceil(n*1.5),"max_weight":1/n}
        values.update(derived)
    changes = dict(overrides or {})
    values.update(changes)
    policy = Policy.model_validate({"mode": mode or PRESETS[preset]["mode"], "parameters": values})
    return {"defaults_version": DEFAULTS_VERSION, "preset": preset,
            "preset_version": 1, "policy": policy.model_dump(mode="json"),
            "mode_provenance": "user" if mode and mode != PRESETS[preset]["mode"] else "preset",
            "provenance": {k: "user" if k in changes else "preset" if k in PRESETS[preset]["overrides"]
                           else "capital_tier" if k in derived else "engineering_default" for k in values},
            "capital_defaults":({**capital,**tier,"parameters":derived,
                "liquidity_note":"流动性门槛为训练资金档位的参考值；当前政策未另行按此门槛过滤股票，不保证成交。"} if capital is not None else None),
            "inactive": ["target_volatility"] if policy.mode == "fixed" else [],
            "changed_from_preset": {**{k: v for k,v in changes.items()
                                    if v != dict(base, **PRESETS[preset]["overrides"],**derived).get(k)},
                                    **({"mode":policy.mode} if policy.mode!=PRESETS[preset]["mode"] else {})}}


def schema():
    value = Policy.model_json_schema()
    definition=next(v for v in value["$defs"].values() if "n_hold" in v.get("properties",{}))
    for f in fields(GovernanceConfig):
        definition["properties"][f.name].update(dict(f.metadata))
    value["defaults_version"]=DEFAULTS_VERSION
    value["ui"] = {key: {"label": label, "unit": unit, "help": help,
                         "applies_when": "mode != fixed" if key == "target_volatility" else "always"}
                   for key,(label,unit,help) in HELP.items()}
    return value
