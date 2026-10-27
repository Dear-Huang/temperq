"""Allowlisted reports for sharing; account rows and local paths never enter."""
import json
import math


def clean(value):
    if isinstance(value, dict): return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list): return [clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value): return None
    return value


def build(state, model_metrics=None):
    spec = state["spec"]
    good = [r for r in state["evaluations"] if r["status"] == "SUCCEEDED"]
    result = {"schema": "strategy_scorecard_v1", "version": spec["vid"],
        "evidence": "INSUFFICIENT", "scope": "historical_development",
        "attempts": len(state["evaluations"]), "historical_accounts": {},
        "limitations": ["历史检验不证明未来收益；未包含持仓明细和账户初值。"]}
    if good:
        result["period"] = {k: good[-1][k] for k in ("start", "end")}
        for arm, values in good[-1]["metrics"].items():
            result["historical_accounts"][arm] = {k: values.get(k) for k in
                ("total_return", "max_drawdown", "mean_exposure", "total_cost", "gross_traded_fraction", "sessions")}
    if model_metrics:
        key=model_metrics.get('model_key','lgbm_ensemble')
        stats=model_metrics.get('ic',{}).get(key,{}).get('final_test',{})
        result['signal']={k:stats.get(k) for k in ('mean','se','t','n')}
    if model_metrics and "rule_card" in model_metrics:
        rules = model_metrics["rule_card"]["rules"]
        result["rules"] = [{k: r[k] for k in ("description", "origin", "required", "weight")} for r in rules]
        result["coverage"] = model_metrics.get("coverage", {})
        result["rule_attempts"] = model_metrics.get("rule_attempts", 0)
        result['changes']=[{k:r[k] for k in ('before','after')} for r in model_metrics.get('rule_changes',[])]
        paired=model_metrics.get('paired_previous',{})
        if paired:
            result['paired_previous']={k:paired[k] for k in ('rank_ic_difference','candidate_cells','reference_cells','scope')}
    return clean(result)


def render(result, format):
    if format == "json": return json.dumps(clean(result), ensure_ascii=False, indent=2, allow_nan=False)
    if format != "md": raise ValueError("仅支持 Markdown 或 JSON")
    lines = ["# 策略成绩卡", "", "结论：证据不足；历史结果不代表未来收益。", "",
             f"本版本账户检验次数：{result['attempts']}", ""]
    if result.get('period'):
        lines += [f"历史期间：{result['period']['start']} 至 {result['period']['end']}", '']
    if 'rule_attempts' in result:
        lines += [f"本系列规则检验次数：{result['rule_attempts']}", '']
    for key,title in (('signal','信号排序'),('coverage','输入覆盖'),('paired_previous','与上一版配对')):
        if result.get(key):
            lines += ['## '+title,'','```json',json.dumps(result[key],ensure_ascii=False,indent=2,allow_nan=False),'```','']
    lines += ["## 历史账户对照", ""]
    for name, values in result["historical_accounts"].items():
        lines += [f"### {name}", "", *(f"- {k}: {v}" for k, v in values.items()), ""]
    lines += ["## 规则及来源", ""]
    for rule in result.get("rules", []):
        lines.append(f"- {rule['description']}；来源 {rule['origin']}；{'必须满足' if rule['required'] else '加分'}；权重 {rule['weight']}")
    if result.get('changes'):
        lines += ['', '## 规则变更', '']
        lines += [f"- {r['before']} → {r['after']}" for r in result['changes']]
    return "\n".join(lines) + "\n"
