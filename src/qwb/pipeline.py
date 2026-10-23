"""One run end to end, from a resolved run config (qwb.runconfig).

load data -> walk-forward training -> baselines and tests -> prediction table
-> report. Used by `qwb run <config.yaml>`; scripts/m2_run.py is a thin wrapper.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import yaml

from . import advice, daily, env, experiments, leakcheck, ledger, metrics, netcost

TZ = {"cn_a": "Asia/Shanghai", "us": "America/New_York"}


def synth_bars(n_days: int = 700, n_codes: int = 300, seed: int = 0) -> pd.DataFrame:
    """Synthetic panel with a weak 5-day reversal; checks the pipeline, not a market."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-01", periods=n_days)
    ret = rng.normal(0, 0.02, (n_days, n_codes))
    for t in range(5, n_days):
        ret[t] -= 0.05 * ret[t - 5:t].sum(axis=0)
    close = 10 * np.exp(np.cumsum(ret, axis=0))
    d = np.repeat(dates, n_codes)
    c = np.tile([f"{i:06d}.SZ" for i in range(n_codes)], n_days)
    cl = close.ravel()
    op = cl * np.exp(rng.normal(0, 0.002, cl.shape))
    return pd.DataFrame({"code": c, "date": d, "open": op, "high": np.maximum(op, cl) * 1.01,
                         "low": np.minimum(op, cl) * 0.99, "close": cl,
                         "amount": 1e7 * (1 + np.tile(np.arange(n_codes), n_days)),
                         "adj_factor": 1.0, "up_limit": cl * 1.1})


def find_local_config(start: Path) -> dict:
    """config.local.yaml in the working directory or any parent; {} if none."""
    for d in [start, *start.parents]:
        p = d / "config.local.yaml"
        if p.is_file():
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


def load_data(rc: dict, cwd: Path) -> tuple[dict, str, pd.DataFrame | None, pd.DataFrame | None]:
    """(panel, snapshot id, ST mask or None, index-member mask or None)."""
    src = rc["data"]
    cfg = rc["cfg"]
    u = cfg["universe"]
    if src["source"] == "synthetic":
        bars = synth_bars(src.get("n_days") or 700, src.get("n_codes") or 300)
        if u not in ("all", "list"):
            raise ValueError("synthetic data has no ST or index data; use universe: all")
        return daily.panel_from_bars(bars), "snap-synthetic", None, None
    data_dir = src.get("dir") or find_local_config(cwd).get("data_dir")
    if not data_dir:
        raise FileNotFoundError("data.dir not set and no data_dir in config.local.yaml")
    from . import dataset
    location = Path(data_dir)
    ds = dataset.load(location if location.is_absolute() else cwd / location)
    P, snap = ds["P"], ds["snapshot_id"]
    dates, codes = P["close"].index, P["close"].columns
    st_all = None if ds["st"] is None else daily.st_wide(ds["st"], dates, codes)
    st = members = None
    if u in ("st", "all_ex_st"):
        if st_all is None:
            raise FileNotFoundError("this dataset has no ST status")
        st = st_all
    elif u in daily.INDEX_CODES:
        if ds["members"] is None:
            raise FileNotFoundError("this dataset has no index constituents "
                                    "(run scripts/fetch_index_members.py)")
        members = daily.members_wide(ds["members"], dates, codes, daily.INDEX_CODES[u])
        if not members.to_numpy().any():
            raise ValueError(f"no {u} constituents fall inside the data range")
        if st_all is not None:                  # be explicit: no ST names in index pools
            members &= ~st_all
    return P, snap, st, members


def _summ(ic, dates, years, lag):
    return metrics.newey_west(np.where(np.isin(dates.year, years), ic, np.nan), lag)


def _fmt(s: dict) -> str:
    if not s["n"]:
        return "n/a"
    return f"{s['mean']:+.4f} (t={s['t']:.2f}, MDE={s['mde']:.4f}, n={s['n']})"


def run(rc: dict, cwd: Path, log: Callable[[str], None] = print) -> dict:
    """Preserve each attempt; refresh the compatibility output only after success."""
    cwd = Path(cwd).resolve()
    target = (cwd / rc["output"]).resolve()
    history = cwd / "runs" / "history"
    if target == history or history in target.parents or target in (cwd, cwd / "runs") or target in cwd.parents:
        raise ValueError("output must be a dedicated run directory outside runs/history")
    if (target / "run.json").is_file() and Path(experiments.read(target / "run.json")["archive_dir"]).resolve() == target:
        raise ValueError("output is an archived training version; choose a latest-output directory")
    out, record = experiments.begin(cwd, rc, target)
    try:
        with experiments.output_lock(target):
            if (target / "metrics.json").is_file():
                previous = experiments.preserve_latest(target, cwd)
                log(f"preserved previous run {previous['run_id']}")
            (out / "models").mkdir()
            resolved = dict(rc, _lineage={k: record[k] for k in ("run_id", "experiment_id", "code_hash")})
            log(f"run_id {record['run_id']}  archive {out}")
            summary = _compute(resolved, cwd, log, out, target)
            experiments.finish(out, record, summary)
            stage = target.parent / f".{target.name}.staging-{record['run_id']}"
            try:
                shutil.copytree(out, stage)
                _publish(stage, target)
            except (OSError, RuntimeError) as exc:
                log(f"latest output was not refreshed: {exc}; archived result: {out}")
            finally:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
    except BaseException as exc:
        if record["state"] != "succeeded":
            from .jobs import Cancelled
            record.update(state="cancelled" if isinstance(exc, (Cancelled, KeyboardInterrupt)) else "failed",
                          finished_at=experiments.now(), error=f"{type(exc).__name__}: {exc}")
            experiments.write(out / "run.json", record)
        raise
    summary["output"] = str(out)
    summary["latest_output"] = str(target)
    return summary


def _retry(op, tries: int = 10):
    """Windows: a reader or a sync client can hold a file for a moment."""
    for i in range(tries):
        try:
            return op()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.3)


def _publish(stage: Path, target: Path) -> None:
    """Move a finished run into place. Files the run does not produce are left alone;
    model folders are swapped whole, so no file from an earlier run lingers."""
    target.mkdir(parents=True, exist_ok=True)
    try:
        for p in sorted(stage.iterdir()):
            dest = target / p.name
            if p.is_dir():
                old = target / f".{p.name}.old-{os.getpid()}"
                if dest.exists():
                    _retry(lambda: dest.rename(old))
                _retry(lambda: p.rename(dest))
                shutil.rmtree(old, ignore_errors=True)
            else:
                _retry(lambda: os.replace(p, dest))
    except OSError as e:
        raise RuntimeError(f"训练已完成，但结果无法放进 {target}（文件被其他程序占用：{e}）。"
                           f"完整结果保存在 {stage}") from e
    shutil.rmtree(stage, ignore_errors=True)


def _compute(rc: dict, cwd: Path, log: Callable[[str], None], out: Path, target: Path) -> dict:
    if rc.get("engine") == "e2e_utility_v1":
        from .training.utility_runner import compute

        return compute(rc, cwd, log, out, target)
    if rc.get("engine") == "e2e_prediction_v1":
        from .training.runner import compute

        return compute(rc, cwd, log, out, target)
    card, cfg = rc["card"], rc["cfg"]
    tz = TZ.get(card["market"], "UTC")
    T = {}
    t0 = time.time()

    # acceptance is locked before the final test years are first scored (SPEC §6)
    acc = json.dumps(card.get("acceptance", {}), sort_keys=True, ensure_ascii=False)
    acc_hash = hashlib.sha256(acc.encode("utf-8")).hexdigest()[:16]
    lock = target / "acceptance.lock"            # kept from the first run of this output
    acc_changed = lock.exists() and lock.read_text(encoding="utf-8").strip() != acc_hash
    (out / "acceptance.lock").write_text(lock.read_text(encoding="utf-8") if lock.exists() else acc_hash,
                                         encoding="utf-8")
    (out / "task.yaml").write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False),
                                   encoding="utf-8")
    (out / "config.resolved.yaml").write_text(yaml.safe_dump(
        {k: rc[k] for k in ("name", "data", "model", "cfg", "config_hash", "deviations")},
        allow_unicode=True, sort_keys=False), encoding="utf-8")

    data_identity = None
    data_root = None
    if rc["data"]["source"] == "snapshot":
        from . import dataset
        location = Path(rc["data"].get("dir") or find_local_config(cwd).get("data_dir") or "")
        data_root = location if location.is_absolute() else cwd / location
        data_identity = dataset.fingerprint(data_root)
        (out / "data_manifest.json").write_text(json.dumps(data_identity, indent=2), encoding="utf-8")
        rc = dict(rc, _data_content_hash=data_identity["content_hash"])
    P, snap, st, members = load_data(rc, cwd)
    T["load"] = time.time() - t0
    dates = P["close"].index
    log(f"panel {len(dates)} days x {P['close'].shape[1]} codes, load {T['load']:.1f}s")
    test_years = sorted({d.year for d in dates if d.year >= cfg["first_test_year"]})
    final = cfg["final_test_years"]
    if not final or not set(final) <= set(test_years):
        raise ValueError("requested final test years are absent from the data; none may be silently dropped")

    t1 = time.time()
    res = daily.run(P, card, cfg, snap, tz, st=st, model_dir=out / "models", log=log,
                    members=members)
    model = cfg.get("model", "lightgbm")
    mk = "lgbm_ensemble" if model == "lightgbm" else model
    T["train_and_predict"] = time.time() - t1

    t2 = time.time()
    y = res["y"]
    lag = int(card["target"].get("horizon_days") or
              (int(card["label"]["end"]["day"]) - int(card["label"]["start"]["day"])))
    seeds = list(res["pred"])
    ens = daily.ensemble(res["pred"])
    test_years = sorted({d.year for d in dates if d.year >= cfg["first_test_year"]})
    final = list(cfg["final_test_years"])
    valid = [yy for yy in test_years if yy < min(final)]
    if not final:
        raise ValueError(f"final_test_years {cfg['final_test_years']} not in the data")

    raw = daily.raw_features(P)
    tested = np.isin(dates.year, test_years)[:, None] & res["U"]
    base = {"reversal_5d (-r5)": -raw["r5"].values, "momentum_20d (r20)": raw["r20"].values,
            "low_vol (-vol20)": -raw["vol20"].values}
    common = tested & np.isfinite(ens) & np.isfinite(y)
    for values in base.values():
        common &= np.isfinite(values)
    ic = {mk: metrics.rank_ic(np.where(common, ens, np.nan), y)}
    if model == "lightgbm":            # ridge is deterministic: seeds are identical
        for s in seeds:
            ic[f"lgbm_seed{s}"] = metrics.rank_ic(np.where(common, res["pred"][s], np.nan), y)
    for k, v in base.items():
        ic[k] = metrics.rank_ic(np.where(common, v, np.nan), y)
    rows = {k: {"validation": _summ(v, dates, valid, lag), "final_test": _summ(v, dates, final, lag)}
            for k, v in ic.items()}
    fin = np.isin(dates.year, final)
    null = metrics.shuffle_null(np.where(fin[:, None], ens, np.nan), y,
                                n=cfg["shuffle_null_draws"])
    # guardrail "IC is not a return": same long-only net-of-cost test for model and baselines
    pf = cfg["portfolio"]
    try:
        net = netcost.evaluate(
            P, card, {mk: ens,
                      **{k: np.where(tested, v, np.nan) for k, v in base.items()}},
            res["U"], y, final, top=pf["top"], commission_bp=pf["commission_bp"],
            n_hold=pf.get("n_hold"))
    except ImportError:
        net = None
        log("net-of-cost check skipped: ashare-tcost not installed")
    # same model under other tools' default backtest rules, one change at a time
    conventions = None
    if net is not None:
        conventions = netcost.compare_conventions(
            P, card, ens, res["U"], y, final, top=pf["top"],
            commission_bp=pf["commission_bp"], n_hold=pf.get("n_hold"))
    seed_corr, seed_overlap = [], []
    for i in range(len(seeds) if model == "lightgbm" else 0):
        for j in range(i + 1, len(seeds)):
            a, b = res["pred"][seeds[i]], res["pred"][seeds[j]]
            seed_corr.append(float(np.nanmean(metrics.rank_ic(a, b))))
            seed_overlap.append(metrics.top_overlap(a, b, 0.1))
    T["evaluate"] = time.time() - t2

    t3 = time.time()
    pred_tab = daily.prediction_table(res, card, snap, tz)
    prediction_version = experiments.object_digest({"fold_models": experiments.inventory(out / "models"),
                                                    "config": rc["config_hash"], "snapshot": snap,
                                                    "data_content_hash": (data_identity or {}).get("content_hash")})
    pred_tab["model_version"] = prediction_version
    pred_tab.to_parquet(out / "predictions.parquet", index=False)
    (out / "folds.json").write_text(json.dumps(res["manifests"], indent=2), encoding="utf-8")
    T["write"] = time.time() - t3
    # the model to use from here on: refit on everything with a matured label
    t4 = time.time()
    final_model = daily.fit_final(P, card, cfg, snap, tz, out / "final_model", res["F"],
                                  res["U"], log=log)
    final_model.update({"cfg": cfg, "card": card, "data": rc["data"], "snapshot_id": snap,
                        "data_end": f"{dates[-1]:%Y-%m-%d}", "run": rc["name"]})
    lineage = dict(rc.get("_lineage", {}), model_version=experiments.model_version(out, rc, snap),
                   prediction_version=prediction_version)
    final_model.update(lineage)
    final_model["data_content_hash"] = (data_identity or {}).get("content_hash")
    (out / "final_model" / "meta.json").write_text(
        json.dumps(final_model, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    T["final_model"] = time.time() - t4
    T["total"] = time.time() - t0

    f = rows[mk]["final_test"]
    screen = metrics.paired_signal_screen(np.where(fin, ic[mk], np.nan),
        {k: np.where(fin, ic[k], np.nan) for k in base}, lag)
    meta = env.metadata()
    summary = {"task": card["name"], "run": rc["name"], "config_hash": rc["config_hash"],
               **lineage,
               "model": model, "model_key": mk, "scenario": cfg.get("scenario"),
               "universe_size": float(res["U"][np.isin(dates.year, final)].sum(axis=1).mean()),
               "data_snapshot_id": snap, "universe": cfg["universe"],
               "data_content_hash": (data_identity or {}).get("content_hash"),
               "selection": {"folds": [m.get("selection", []) for m in res["manifests"]],
                             "final_refit": final_model.get("selection", [])},
               "panel": [len(dates), int(P["close"].shape[1])], "test_years": test_years,
               "final_test_years": final, "nw_lag": lag, "ic": rows,
               "validation_years": valid, "post_test_years": [yy for yy in test_years if yy > max(final)],
               "shuffle_null_mean": float(np.mean(null)), "shuffle_null_sd": float(np.std(null)),
               "seed_pred_rank_corr": seed_corr, "seed_top10_overlap": seed_overlap,
               "net_of_cost": net, "conventions": conventions,
               "go": False, "signal_screen": screen,
               "signal_sample": {"rule": "same_stock_date_pairs_for_candidate_and_all_simple_references",
                                 "eligible_pairs": int((tested & np.isfinite(y)).sum()),
                                 "common_pairs": int(common.sum())},
               "acceptance_hash": acc_hash, "acceptance_changed_after_lock": acc_changed,
               "deviations": rc["deviations"], "timing_s": {k: round(v, 1) for k, v in T.items()},
               "env": meta, "rows_predicted": int(len(pred_tab)),
               "final_model": {k: final_model[k] for k in ("model", "train_end", "n_train", "data_end")},
               "advice": advice.check(cfg, card, len(set(dates.year)))}
    # guardrails: look-ahead check, and how often this test period has been scored
    summary["leak_check"] = leakcheck.prefix_invariance(P, cfg, st, F=res["F"], U=res["U"],
                                                       members=members)
    summary["go"] = bool(screen["status"] == "PASS" and summary["leak_check"]["ok"]
                         and not acc_changed and snap != "snap-synthetic")
    if data_root is not None and dataset.fingerprint(data_root) != data_identity:
        raise ValueError("training data changed during the run; result is not a fixed-data experiment")
    # synthetic data has no test period worth protecting: installer smoke tests and
    # trial runs must not make a user's first real run look like a repeat view
    summary["ledger"] = None
    if snap != "snap-synthetic":
        led = cwd / "runs" / "ledger.jsonl"
        summary["ledger"] = ledger.history(led, card["name"], snap, final, rc["config_hash"],
                                           f["mean"])
        ledger.append(led, {"run": rc["name"], **lineage, "task": card["name"], "snapshot": snap,
                            "config_hash": rc["config_hash"], "final_test_years": final,
                            "final_ic": f["mean"], "final_t": f["t"],
                            "net_excess": (net or {}).get("signals", {}).get(mk, {})
                            .get("net_excess_ann"), "deviations": rc["deviations"]})
    (out / "metrics.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (out / "report.md").write_text(_report(rc, summary, rows, null, seed_corr, seed_overlap,
                                           valid, final, T, meta, len(seeds)), encoding="utf-8")
    return summary


def _guard_lines(s: dict) -> list[str]:
    lc, lg = s.get("leak_check"), s.get("ledger")
    out = []
    if lc:
        out.append(f"- 前视检查（截断数据重算特征与股票池，{len(lc['days'])} 天 × {lc['items']} 项）："
                   + ("通过" if lc["ok"] else
                      "**未通过** " + "; ".join(f"{v['date']} {v['item']} {v['cells']} 格"
                                              for v in lc["violations"][:5])))
    if str(s.get("data_snapshot_id")) == "snap-synthetic":
        out.append("- 评估台账：试运行数据不记入")
    if lg:
        msg = (f"- 最终测试期已被评估 {lg['views']} 次（{lg['configs']} 种配置，本地台账自报，不作认证）")
        if lg["views"] > 1:
            msg += "；**这不是首次查看，结果不能当作独立确认**"
            if lg["is_best"]:
                msg += "；本次是其中最好的一次，存在挑选偏差"
        if lg["years_changed"]:
            msg += f"；**测试期与首次运行 {lg['first_final_years']} 不同**"
        out.append(msg)
    return out


def _ledger_table(s: dict) -> list[str]:
    """Every earlier scoring of this test period, so no configuration can be left out."""
    lg = s.get("ledger")
    if not lg or not lg["earlier"]:
        return []
    lines = ["## 同一测试期的全部评估记录（本地台账）", "",
             "| 时间 | 运行 | 配置 | 最终测试 IC | 净超额 | 偏离默认值 |", "|---|---|---|---|---|---|"]
    for e in lg["earlier"]:
        ne = e.get("net_excess")
        lines.append(f"| {e.get('t', '')[:16]} | {e.get('run', '')} | `{str(e.get('config_hash', ''))[-8:]}` | "
                     f"{e.get('final_ic', float('nan')):+.4f} | "
                     f"{'n/a' if ne is None else f'{ne:+.1%}'} | "
                     f"{'; '.join(e.get('deviations') or []) or '无'} |")
    fic = s["ic"][s.get("model_key", "lgbm_ensemble")]["final_test"]["mean"]
    lines.append(f"| 本次 | {s['run']} | `{s['config_hash'][-8:]}` | {fic:+.4f} | "
                 f"{_net_str(s)} | {'; '.join(s['deviations']) or '无'} |")
    if lg["earlier_total"] > len(lg["earlier"]):
        lines.append(f"\n只列出最近 {len(lg['earlier'])} 条，共 {lg['earlier_total']} 条。")
    return lines + [""]


def _net_str(s: dict) -> str:
    v = (s.get("net_of_cost") or {}).get("signals", {}).get(s.get("model_key", "lgbm_ensemble"))
    return "n/a" if not v else f"{v['net_excess_ann']:+.1%}"


def _scenario_lines(cfg: dict) -> list[str]:
    sc = cfg.get("scenario")
    if not sc:
        return []
    out = ["## 应用场景", ""]
    for k, lbl in (("market", "市场"), ("horizon", "持有期"), ("universe", "股票池"),
                   ("capital", "资金量"), ("model", "模型")):
        if sc.get(k):
            out.append(f"- {lbl}：{sc[k]}")
    if cfg.get("min_adv_yuan"):
        out.append(f"- 由资金量推出：持有约 {cfg['portfolio'].get('n_hold', '—')} 只，"
                   f"只选近 20 日日均成交额 ≥ {cfg['min_adv_yuan'] / 1e4:,.0f} 万元的股票"
                   "（单票仓位不超过其日成交额的 5%）")
    return out + [""]


def _convention_lines(conv: dict | None) -> list[str]:
    if not conv:
        return []
    lines = ["## 换成常见回测口径会怎样（同一个模型，每次只改一处）", "",
             "Qlib 官方 LightGBM 基准的默认设置：收盘价成交、所有板块统一按 9.5% 涨跌停、"
             "固定费率（买 5bp、卖 15bp）。下表从 qwb 的可成交口径出发，逐项换成这些设置。", "",
             "| 口径 | 毛超额 | 成本 | 净超额 | 净超额 t |", "|---|---|---|---|---|"]
    for k, v in conv.items():
        lines.append(f"| {k} | {v['gross_excess_ann']:+.1%} | {v['cost_ann']:.1%} | "
                     f"{v['net_excess_ann']:+.1%} | {v['net_excess_t']:.2f} |")
    v = list(conv.values())
    first, close_fill, limits, last = v[0], v[1], v[2], v[-1]
    gap = last["net_excess_ann"] - first["net_excess_ann"]
    cost_gap = first["cost_ann"] - last["cost_ann"]
    fill_gap = close_fill["gross_excess_ann"] - first["gross_excess_ann"]
    limit_gap = limits["gross_excess_ann"] - close_fill["gross_excess_ann"]
    lines += ["", f"按 Qlib 基准口径，净超额 {last['net_excess_ann']:+.1%}/年，qwb 口径 "
              f"{first['net_excess_ann']:+.1%}/年，相差 {gap:+.1%}。拆开看：固定费率比个股价差估计"
              f"{'少算' if cost_gap > 0 else '多算'}成本 {abs(cost_gap):.1%}/年；收盘价成交让毛超额变化 "
              f"{fill_gap:+.1%}；统一 9.5% 涨跌停让毛超额变化 {limit_gap:+.1%}。"
              "几处差异方向不同，可能互相抵消，所以要看分项，不只看合计。", ""]
    return lines


def _net_section(net: dict | None, cfg: dict, final: list[int]) -> list[str]:
    if not net:
        return []
    pf = cfg["portfolio"]
    lines = [f"## 扣成本后（最终测试 {final[0]}–{final[-1]}，年化）", "",
             f"做法：每 {net['hold_days']} 个交易日按信号买入"
             + (f"排名前 {pf['n_hold']} 只" if pf.get("n_hold") else f"股票池前 {pf['top']:.0%}")
             + "，等权，"
             f"按标签的开仓价成交、持有到标签平仓价；涨停或停牌买不进，跌停或停牌卖不出则继续持有。"
             f"成本＝STRUCT 半价差（上月日频数据估计，ashare-tcost）＋佣金 {pf['commission_bp']:g}bp/边"
             f"＋卖出印花税（规则包）。超额相对股票池等权（年化 {net['benchmark_ann']:+.1%}）。", "",
             "| 信号 | 毛超额 | 成本 | **净超额** | 净超额 t | 单边换手/期 |", "|---|---|---|---|---|---|"]
    for k, v in net["signals"].items():
        lines.append(f"| {k} | {v['gross_excess_ann']:+.1%} | {v['cost_ann']:.1%} | "
                     f"**{v['net_excess_ann']:+.1%}** | {v['net_excess_t']:.2f} | "
                     f"{v['turnover_one_way']:.0%} |")
    lines += ["", "未计入大单冲击成本；资金规模越大，真实成本越高于此表。", ""]
    return lines


def _report(rc, s, rows, null, seed_corr, seed_overlap, valid, final, T, meta, n_seeds) -> str:
    card, cfg = rc["card"], rc["cfg"]
    devs = rc["deviations"]
    g = meta["gpu"]
    synthetic = rc["data"]["source"] == "synthetic"
    lines = [
        f"# 报告：{rc['name']}{'（合成数据，只验证流程）' if synthetic else ''}", "",
        f"任务 `{card['name']}`　数据快照 `{s['data_snapshot_id']}`　股票池 `{cfg['universe']}`　"
        f"面板 {s['panel'][0]} 日 × {s['panel'][1]} 只（测试期股票池平均 {s.get('universe_size', 0):.0f} 只）　"
        + (f"模型 LightGBM × {n_seeds} 种子" if s.get("model", "lightgbm") == "lightgbm"
           else "模型 Ridge 线性回归") + "，按年滚动重训　"
        f"配置 `{rc['config_hash']}`", "",
        *_scenario_lines(cfg),
        *_net_section(s.get("net_of_cost"), cfg, final),
        *_convention_lines(s.get("conventions")),
        "## 首页标注",
        f"- 验收门槛哈希 `{s['acceptance_hash']}`"
        + ("；**门槛在锁定后被修改**" if s["acceptance_changed_after_lock"] else "，锁定后未改"),
        "- 偏离默认值：" + ("; ".join(devs) if devs else "无"),
        "- 未执行：冲击成本（大单）、优化型组合构建" if s.get("net_of_cost")
        else "- 未执行：扣成本检验（未安装 ashare-tcost），以下 IC 不代表收益",
        *_guard_lines(s), "",
        *([f"- 最终测试之后的年份 {s['post_test_years']} 不用于验证期汇总或选择；"
           "这些年份仍可生成滚动预测，并进入面向未来的最终重拟合。", ""]
          if s.get("post_test_years") else []),
        *(["## 历史内层模型选择", "",
           "每个外层折仅在其训练历史内，以尾部交易日的平均 Rank IC 选择 LightGBM 轮数。"
           "内层训练清除未在首个验证决策时点成熟的标签；选择后在全部外层已成熟样本上重拟合。"
           "测试期不参与轮数选择。最终推理模型另用截至数据末日的历史选择与重拟合，未得到独立前向检验。", "",
           "逐折日期、样本数、清除数量与每个种子的轮数见 folds.json；最终重拟合记录见 final_model/meta.json。", ""]
          if cfg.get("selection") else []),
        *advice.as_markdown(s.get("advice") or []),
        f"## 结果（日度 Rank IC 均值，Newey-West t，lag 事先固定为 {s['nw_lag']}；"
        "MDE 为 80% 功效下可检出的最小均值）", "",
        f"| 信号 | 验证期 {valid[0] if valid else ''}–{valid[-1] if valid else ''} | "
        f"最终测试 {final[0]}–{final[-1]} |", "|---|---|---|"]
    lines += [f"| {k} | {_fmt(r['validation'])} | {_fmt(r['final_test'])} |" for k, r in rows.items()]
    lines += ["", f"随机信号基线（截面内打乱，{cfg['shuffle_null_draws']} 次）：最终测试 IC "
              f"{np.mean(null):+.4f} ± {np.std(null):.4f}"]
    if seed_corr:
        lines.append(f"种子噪声地板：种子间预测秩相关 {np.mean(seed_corr):.3f}，前 10% 持仓重叠 "
                     f"{np.mean(seed_overlap):.3f}")
    lines += ["", "## 相对固定参考的增量检验", "",
              "候选与三个简单参考使用相同股票/日期样本；逐日 IC 差采用 Newey-West 标准误，"
              "95% 正态近似区间对三项固定比较做 Bonferroni 校正，不校正全部调参/重复查看。", "",
              "| 参考 | 新减参考 IC | 同组 95% 区间 | 状态 |", "|---|---|---|---|"]
    for name, value in s["signal_screen"]["comparisons"].items():
        ci = value["ci95_family"]
        interval = "n/a" if ci is None else f"[{ci[0]:+.4f}, {ci[1]:+.4f}]"
        lines.append(f"| {name} | {value['mean']:+.4f} | {interval} | {value['status']} |")
    lines += ["", f"**历史信号筛选：{s['signal_screen']['status']}**；"
              "只表示正 IC 与相对上述参考的历史增量证据，不是实盘准入。",
              f"任务卡原始声明（仅记录，不解析执行）：{card.get('acceptance', {}).get('go', '未写门槛')}。",
              "本地记录和门槛哈希不构成独立预注册；重复查看的测试期仍属于开发证据。", ""]
    lines += _ledger_table(s)
    lines += ["## 耗时（秒）", "", "| 阶段 | 秒 |", "|---|---|"]
    lines += [f"| {k} | {v:.1f} |" for k, v in T.items()]
    lines += ["", f"环境：Python {meta['python']}，"
              + (f"{g['name']}（LightGBM 走 CPU，GPU 未使用）" if g else "无 GPU")
              + f"，lightgbm {meta['packages'].get('lightgbm')}", "",
              "## 证据边界",
              "IC 与配对检验描述历史排序信号。训练报告中的净收益是简化诊断，"
              "独立现金/实际保留数量账户、拒单与数据资格检查以 QW-Bench 为准；均不代表实际可实现收益。"]
    return "\n".join(lines)
