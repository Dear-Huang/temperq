"""Pre-registered tests of experience thresholds (docs/specs/AUDIT_EVIDENCE_SPEC.md R7).

A threshold leaves "经验阈值，未经样本检验" only through a trial registered and frozen before
any validation result is seen:

1. ``register`` writes the plan once: rule, every candidate value, development and validation
   periods, the primary metric, the drawdown constraint, how strategies are pooled and the pass
   bar. The file carries a hash; any later edit is detected.
2. ``evaluate`` takes every per-strategy result, including failed runs. The candidate is chosen
   on the development period only, then compared with the default on the validation period.
   The validation period may be looked at once; a second look is refused.
3. The report lists every candidate, so the number of comparisons is visible, and keeps failures.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from . import audit_rules

REQUIRED = {"id", "rule", "default", "candidates", "development", "validation", "primary_metric",
            "drawdown_tolerance", "pooling", "pass"}


def _digest(plan):
    body = {k: v for k, v in plan.items() if k not in {"sha256", "frozen_at"}}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def register(path, plan):
    """Freeze a trial plan. Refuses to overwrite: a changed plan is a new trial with a new id."""
    path = Path(path)
    if path.exists():
        raise ValueError("登记文件已存在；改计划请另起一个新编号的试验")
    missing = REQUIRED - set(plan)
    if missing:
        raise ValueError("登记缺少：" + "、".join(sorted(missing)))
    rule = audit_rules.RULES.get(plan["rule"])
    if rule is None or rule.kind != "threshold":
        raise ValueError(f"只能检验经验阈值：{plan['rule']}")
    candidates = list(plan["candidates"])
    if plan["default"] not in candidates or len(set(candidates)) != len(candidates) or len(candidates) < 2:
        raise ValueError("候选值须互不相同、至少两个，并包含默认值")
    low, high = rule.range
    if any(not low <= c <= high for c in candidates):
        raise ValueError(f"候选值须在 {low}–{high} 之间")
    dev, val = plan["development"], plan["validation"]
    if not dev["start"] <= dev["end"] < val["start"] <= val["end"]:
        raise ValueError("开发段须整体早于验证段，且不重叠")
    frozen = {**plan, "max_validation_views": 1, "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    frozen["sha256"] = _digest(frozen)
    path.write_text(json.dumps(frozen, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return frozen


def load(path):
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if "frozen_at" not in plan or plan.get("sha256") != _digest(plan):
        raise ValueError("登记文件未冻结或已被改动，不能运行验证段")
    return plan


def _views(path):
    log = Path(str(path) + ".views.json")
    return log, (json.loads(log.read_text(encoding="utf-8")) if log.exists() else [])


def evaluate(path, results):
    """results: one row per strategy × candidate × segment:
    {"strategy", "candidate", "segment": "development"|"validation", "status": "ok"|"failed", "metric", "drawdown"}."""
    plan = load(path)
    candidates = plan["candidates"]
    unknown = {r["candidate"] for r in results} - set(candidates)
    if unknown:
        raise ValueError(f"结果里有未登记的候选值：{sorted(unknown)}")
    log, views = _views(path)
    if len(views) >= plan["max_validation_views"]:
        raise ValueError("验证段已经看过一次；登记规定只看一次，不能再次评估")
    views.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rows": len(results)})
    log.write_text(json.dumps(views, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    def table(segment):
        rows = {}
        for r in results:
            if r["segment"] == segment:
                rows.setdefault(r["strategy"], {})[r["candidate"]] = r
        return rows

    dev, val = table("development"), table("validation")
    strategies = sorted(set(dev) | set(val))
    failed = sorted({r["strategy"] for r in results if r.get("status") != "ok"})
    default = plan["default"]

    def pooled(rows, candidate):
        """Median improvement over the default and share of strategies improved; failures count as not improved."""
        diffs, improved, worse_dd = [], 0, 0
        for s in strategies:
            base, cand = rows.get(s, {}).get(default), rows.get(s, {}).get(candidate)
            ok = base and cand and base.get("status") == "ok" and cand.get("status") == "ok"
            if not ok:
                continue
            diff = cand["metric"] - base["metric"]
            diffs.append(diff)
            improved += diff > 0
            worse_dd += (base["drawdown"] - cand["drawdown"]) > plan["drawdown_tolerance"]   # drawdowns are negative
        n = len(strategies)
        return {"candidate": candidate, "median_diff": statistics.median(diffs) if diffs else None,
                "improved_share": improved / n if n else 0., "drawdown_worse_share": worse_dd / n if n else 0.,
                "compared": len(diffs)}

    dev_rows = [pooled(dev, c) for c in candidates if c != default]
    chosen = max(dev_rows, key=lambda r: (r["median_diff"] is not None, r["median_diff"] or 0.), default=None)
    val_rows = [pooled(val, c) for c in candidates if c != default]
    outcome = next((r for r in val_rows if chosen and r["candidate"] == chosen["candidate"]), None)
    bar = plan["pass"]
    passed = bool(outcome and outcome["median_diff"] is not None and outcome["median_diff"] > 0
                  and outcome["improved_share"] >= bar["min_improved_share"]
                  and outcome["drawdown_worse_share"] <= bar["max_drawdown_worse_share"])
    return {"trial": plan["id"], "rule": plan["rule"], "sha256": plan["sha256"], "candidates_tested": len(candidates) - 1,
            "strategies": len(strategies), "failed_strategies": failed,
            "chosen_on_development": chosen and chosen["candidate"], "development": dev_rows, "validation": val_rows,
            "passed": passed,
            "conclusion": (f"{plan['rule']} 改为 {chosen['candidate']} 在验证段通过登记标准；可引用本报告把规则来源改为已检验"
                           if passed else f"{plan['rule']} 未通过登记标准，保持经验阈值；本报告作为反例保留"),
            "note": f"共比较 {len(candidates) - 1} 个候选，候选只在开发段选出；验证段只看一次；失败的策略按未改善计入"}
