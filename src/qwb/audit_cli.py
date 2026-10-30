"""`qwb audit`: the strategy audit without the web page.

    qwb audit check <材料目录>              import only: what each file was read as, whether the books close
    qwb audit run   <材料目录> --out <目录>  full audit: report.html, summary.json, evidence.json, evidence CSVs

Both read a directory of platform exports or standard-format CSVs (see docs/AUDIT_SKILL_FORMAT.md).
Every figure in the outputs comes from the audit engine; nothing here estimates or rewrites a number.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ACCEPTED = (".csv", ".txt", ".py", ".zip", ".json", ".xlsx")


def add_parser(sub):
    parser = sub.add_parser("audit", help="audit a backtest from exported files (no web page)")
    commands = parser.add_subparsers(dest="audit_cmd", required=True)
    for name, text in (("check", "import only; report what was recognised and whether the books close"),
                       ("run", "full audit; write report.html, summary.json, evidence.json and CSVs")):
        cmd = commands.add_parser(name, help=text)
        cmd.add_argument("materials", help="directory of exported files")
        cmd.add_argument("--name", default="", help="strategy name shown in the report")
        cmd.add_argument("--opening-cash", type=float, help="declare an empty opening account with this cash")
        if name == "run":
            cmd.add_argument("--out", required=True, help="new output directory")
            cmd.add_argument("--commission-bp", type=float, help="commission scenario in bp (default: from code, else 3)")
            cmd.add_argument("--min-commission", type=float, help="minimum commission in yuan (default: from code, else 5)")
            cmd.add_argument("--max-weight", type=float, help="single-name cap in percent (default: 100)")
            cmd.add_argument("--broker-confirmed", action="store_true", help="the commission given is the broker contract rate")
            cmd.add_argument("--premise-text", default="", help="the user's own premise, one sentence")
            cmd.add_argument("--premise-direction", choices=("higher", "lower"), help="higher or lower scores are better")
            cmd.add_argument("--claimed-total", type=float, help="claimed total return in percent, e.g. 520 for 520%%")
            cmd.add_argument("--claimed-annual", type=float, help="claimed annual return in percent")
            cmd.add_argument("--claim-source", default="", help="where the claim comes from, e.g. a post title")
        cmd.add_argument("--json", action="store_true", help="print the machine-readable result only")
    cmp = commands.add_parser("compare", help="original vs optimized: was the change worth it (from two qwb audit run outputs)")
    cmp.add_argument("original", help="output directory of the original version's qwb audit run")
    cmp.add_argument("optimized", help="output directory of the optimized version's qwb audit run")
    cmp.add_argument("--sensitivity", action="store_true",
                     help="the two runs differ in the engine model (matching, slippage), not the strategy: "
                          "describe the change, never pick a version by return")
    cmp.add_argument("--json", action="store_true")
    fac = commands.add_parser("interaction", help="two changes alone and together: can the suggestions be added up")
    for name in ("base", "only_a", "only_b", "both"):
        fac.add_argument(name, help=f"qwb audit run output directory: {name}")
    fac.add_argument("--labels", default="A,B", help="names of the two changes, e.g. 保留带,留现金")
    fac.add_argument("--json", action="store_true")
    inst = commands.add_parser("instrument-check", help="prove an instrumented copy only adds TemperQ recording")
    inst.add_argument("original", help="the strategy as the user wrote it")
    inst.add_argument("instrumented", help="the copy with temperq_record(...) added")
    inst.add_argument("--json", action="store_true")


def _files(directory):
    from .strategy_imports import SourceFile
    folder = Path(directory)
    if not folder.is_dir():
        raise ValueError(f"{directory} 不是目录")
    paths = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ACCEPTED)
    skipped = sorted(p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() not in ACCEPTED)
    if not paths:
        raise ValueError(f"{directory} 里没有 CSV/TXT/PY/ZIP 文件")
    return [SourceFile(p.name, p.read_bytes()) for p in paths], skipped


def load(args):
    from . import strategy_imports as imports
    files, skipped = _files(args.materials)
    bundle = imports.normalize(files, adapter="auto", name=(args.name or Path(args.materials).resolve().name)[:80],
                               opening_cash=args.opening_cash)
    return bundle, skipped


def check(args):
    """What the engine read; used to verify a format conversion before the full audit."""
    from . import audit_input, strategy_audit as audit
    bundle, skipped = load(args)
    review = audit_input.review(bundle)
    out = {"status": "imported", "platform": review.get("platform"), "mode": review["mode"],
           "files": [{"name": s["name"], "read_as": s["kind"], "rows": s["rows"]} for s in bundle["sources"]],
           "skipped_files": skipped,
           "counts": {k: len(bundle.get(k) or []) for k in ("fills", "positions", "snapshots", "scores", "prices", "logs")},
           "period": [bundle["snapshots"][0]["date"], bundle["snapshots"][-1]["date"]] if bundle["snapshots"] else None,
           "parameters": {p["key"]: {"value": p["value"], "source": p["source"]} for p in review["parameters"]},
           "checks": [{"title": c["title"], "status": c["status"], "detail": c["detail"]} for c in review["checks"]]}
    if review["mode"] == "full":
        account = audit.accounting(bundle)
        out["books"] = account["status"]
        out["book_failures"] = [{"check": s["key"], "failures": s["failures"], "of": s["count"]}
                                for s in account["summary"] if s["failures"]]
        out["first_exceptions"] = [{k: e.get(k) for k in ("key", "date", "code", "gap")} for e in account["exceptions"][:5]]
    return out


def settings_for(bundle, args):
    from . import audit_input
    found = {p["key"]: p["value"] for p in audit_input.review(bundle)["parameters"]}
    pick = lambda given, key, default: float(given if given is not None else
                                             found.get(key) if isinstance(found.get(key), (int, float)) else default)
    settings = {"commission_bp": pick(args.commission_bp, "commission_bp", 3.),
                "minimum_commission": pick(args.min_commission, "minimum_commission", 5.),
                "max_weight": (args.max_weight if args.max_weight is not None else 100.) / 100,
                "broker_confirmed": bool(args.broker_confirmed)}
    if args.claimed_total is not None or args.claimed_annual is not None:
        settings["claim"] = {"total": None if args.claimed_total is None else args.claimed_total / 100,
                             "annual": None if args.claimed_annual is None else args.claimed_annual / 100,
                             "source": args.claim_source.strip()[:200]}
    if args.premise_text or args.premise_direction:
        settings["premise"] = {"text": args.premise_text.strip()[:200], "direction": args.premise_direction or ""}
    return settings


def summary(bundle, result):
    """The audit's own conclusions in machine-readable form: what the skill may quote, nothing more."""
    from . import audit_actions, audit_attribution, audit_report, audit_verdicts
    perf = audit_report.performance(bundle)
    cards = audit_actions.cards(result, bundle)
    verdicts = audit_verdicts.conclusions(bundle, result)
    premise = result["modules"].get("premise")
    attr = audit_attribution.build(result) if result.get("mode", "full") == "full" else None
    return {
        "schema": "qwb_audit_summary_v1", "strategy": bundle["name"], "mode": result.get("mode", "full"),
        "period": [bundle["snapshots"][0]["date"], bundle["snapshots"][-1]["date"]] if bundle["snapshots"] else None,
        "input_digest": result["input_digest"], "result_digest": result["result_digest"],
        "settings": result.get("settings"),
        "conclusions": [{"tone": tone, "text": text} for tone, text in
                        audit_report.headline(bundle, result, perf, premise, cards, verdicts)],
        "books": (result["modules"].get("accounting") or {}).get("status"),
        "performance": perf and {k: perf.get(k) for k in ("total", "annual", "volatility", "sharpe", "max_drawdown",
                                                          "benchmark_total", "excess_total", "information_ratio", "basis")},
        "platform_check": audit_report.platform_check(bundle, perf) if perf else [],
        "claim_check": audit_report.claim_check(result, perf),
        "premise": premise and {k: premise.get(k) for k in ("premise", "source", "status", "verdict", "overall",
                                                            "spread_mean", "spread_t", "held_years", "reversed_years")},
        "attribution": attr and attr["summary"],
        "sequence": (lambda s: s and {k: (s.get(k) or {}).get("verdict") for k in ("funding", "signal_after_fill", "code_hints")})(
            result["modules"].get("sequence")),
        "fill_quality": (lambda q: q and {"status": q.get("status"),
                                          "range": (q.get("range") or {}).get("verdict"), "floor": (q.get("floor") or {}).get("verdict")})(
            result["modules"].get("fill_quality")),
        "fulfilment": (lambda f: f and {"by_target": (f.get("by_target") or {}).get("verdict"),
                                         "overnight": f["overnight"]["verdict"]})(result["modules"].get("fulfilment")),
        "capital": (lambda c: c and {"status": c.get("status"), "verdict": c.get("verdict")})(result["modules"].get("capital")),
        "allocation": (lambda a: a and {"status": a.get("status"), "verdict": a.get("verdict")})(result["modules"].get("allocation")),
        "activity": (lambda a: a and {"exits": (a.get("exits") or {}).get("verdict"), "idle": a["idle"]["verdict"],
                                       "status": a.get("status")})(result["modules"].get("activity")),
        "partial_entry": (lambda c: c and {k: c.get(k) for k in ("status", "verdict", "partial_buys", "long_episodes", "worst")})(
            result["modules"].get("partial_entry")),
        "fill_timing": (lambda t: t and {k: t.get(k) for k in ("status", "verdict", "open_share", "low", "high", "net_pnl",
                                                               "max_volume_share", "scope")})(result["modules"].get("fill_timing")),
        "findings": [{"title": c["title"], "state": c["state"], "evidence": c["evidence"], "headline": c["headline"],
                      "finding": c.get("finding"), "overturn": c.get("overturn")} for c in cards],
        "stages": [{"title": v["title"], "state": v["state"], "reason": v["reason"]} for v in verdicts],
        "scope": audit_report.scope(result),
    }


def run(args):
    from . import audit_exports, audit_report, experiments as ex, strategy_audit as audit
    from .governance_review_ui import csv_text
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"{out} 已存在且不为空；审计输出不覆盖旧结果")
    bundle, skipped = load(args)
    settings = settings_for(bundle, args)
    result = audit.analyze(bundle, settings)
    result["confirmation"] = {"same_run": True, "settings": settings, "via": "qwb audit run",
                              "scope": "report_assumptions_only_not_live_rules"}
    result["result_digest"] = ex.object_digest({k: v for k, v in result.items() if k != "result_digest"})
    out.mkdir(parents=True, exist_ok=True)
    record = {"created_at": _now(), "skipped_files": skipped}
    document = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>'
                + audit_report.render(record, bundle, result) + "</body></html>")
    (out / "report.html").write_text(document, encoding="utf-8")
    brief = summary(bundle, result)
    (out / "summary.json").write_text(json.dumps(brief, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (out / "evidence.json").write_text(json.dumps({"record": record, "input": bundle, "result": result},
                                                  ensure_ascii=False, default=str), encoding="utf-8")
    written = ["report.html", "summary.json", "evidence.json"]
    for kind in audit_exports.available(result):
        rows, columns = audit_exports.table(result, kind)
        (out / f"{kind}.csv").write_text("﻿" + csv_text(rows, columns), encoding="utf-8")
        written.append(f"{kind}.csv")
    return {"status": "done", "out": str(out), "files": written, "conclusions": brief["conclusions"],
            "books": brief["books"], "skipped_files": skipped}


def _evidence(directory):
    path = Path(directory) / "evidence.json"
    if not path.exists():
        raise ValueError(f"{directory} 里没有 evidence.json；先对这一版运行 qwb audit run")
    return json.loads(path.read_text(encoding="utf-8"))["result"]


def compare_runs(original_dir, optimized_dir, sensitivity=False):
    """The engine's same-method comparison of two audited versions; nothing is computed here."""
    from . import audit_verification
    loaded = []
    for d in (original_dir, optimized_dir):
        path = Path(d) / "evidence.json"
        if not path.exists():
            raise ValueError(f"{d} 里没有 evidence.json；先对这一版运行 qwb audit run")
        loaded.append(json.loads(path.read_text(encoding="utf-8"))["result"])
    a, b = loaded
    if (a.get("settings") or {}).get("commission_bp") != (b.get("settings") or {}).get("commission_bp") or             (a.get("settings") or {}).get("minimum_commission") != (b.get("settings") or {}).get("minimum_commission"):
        raise ValueError("两版的费用情景不同，不能同口径比较；用相同的 --commission-bp 和 --min-commission 重跑其中一版")
    out = audit_verification.compare(a, b)
    if out is None:
        raise ValueError("其中一版账目未闭合或材料不全，无法比较")
    sources = [json.loads((Path(d) / "evidence.json").read_text(encoding="utf-8"))["input"].get("code", {}).get("source")
               for d in (original_dir, optimized_dir)]
    same_code = bool(sources[0]) and sources[0] == sources[1]
    digests = [json.loads((Path(d) / "evidence.json").read_text(encoding="utf-8"))["result"].get("input_digest")
               for d in (original_dir, optimized_dir)]
    if digests[0] and digests[0] == digests[1]:
        return {"comparable": True, "mode": "identical", "verdict": "两次审计的输入完全相同：没有可比较的变化"}
    sensitivity = sensitivity or same_code
    keep = ("comparable", "reason", "start", "end", "same_period", "full_gain", "validation_gain", "drawdown",
            "same_trades", "drawdown_deeper", "evidence", "verdict", "state")
    out = {k: out.get(k) for k in keep if k in out}
    if sensitivity:
        from .audit_report import performance
        a_perf, b_perf = (performance(json.loads((Path(d) / "evidence.json").read_text(encoding="utf-8"))["input"])
                          for d in (original_dir, optimized_dir))
        out.pop("state", None)
        out["mode"] = "model_sensitivity"
        out["reported"] = {"total": [a_perf and a_perf["total"], b_perf and b_perf["total"]],
                           "max_drawdown": [a_perf and a_perf["max_drawdown"], b_perf and b_perf["max_drawdown"]]}
        change = (f"回测原值：收益从 {a_perf['total']:.1%} 变为 {b_perf['total']:.1%}，"
                  f"最大回撤从 {a_perf['max_drawdown']:.1%} 变为 {b_perf['max_drawdown']:.1%}") if a_perf and b_perf else ""
        out["verdict"] = (change + ("。两版策略代码完全相同，差异来自代码以外的设置或数据（撮合、滑点、量限、数据版本等），"
                                    "按敏感性实验处理" if same_code else "。按敏感性实验处理")
                          + "：结果变化说明结论对这些设置有多敏感；不能按收益高低判断哪个设置更真实，也不要据此选版本"
                          + ("。两次交易完全相同" if out.get("same_trades") else ""))
    else:
        out["note"] = "full_gain 和 drawdown 按审计费率与价差情景重算，不是回测原值"
    return out


def _now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def main(args):
    try:
        if args.audit_cmd == "compare":
            result = compare_runs(args.original, args.optimized, sensitivity=args.sensitivity)
            print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 1, default=str))
            return 0 if result.get("comparable") else 1
        if args.audit_cmd == "interaction":
            from . import audit_factorial
            runs = [_evidence(d) for d in (args.base, args.only_a, args.only_b, args.both)]
            labels = tuple((args.labels.split(",") + ["A", "B"])[:2])
            result = audit_factorial.analyze(*runs, labels=labels)
            print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 1, default=str))
            return 0 if result.get("comparable") else 1
        if args.audit_cmd == "instrument-check":
            from . import audit_instrument
            from .strategy_imports import decode_source, SourceFile
            read = lambda p: decode_source(SourceFile(Path(p).name, Path(p).read_bytes()))[0]
            result = audit_instrument.compare(read(args.original), read(args.instrumented))
            print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 1))
            return 0 if result["same_logic"] else 1
        result = check(args) if args.audit_cmd == "check" else run(args)
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    if args.audit_cmd == "check" and result.get("books") not in (None, "verified"):
        return 1
    return 0


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(prog="qwb")
    add_parser(p.add_subparsers(dest="cmd", required=True))
    sys.exit(main(p.parse_args()))
