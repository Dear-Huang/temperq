"""Risk-only historical diagnostic; no shares, portfolio NAV or forward record.

Uses governance's unchanged causal risk head and common top-N reference basket.
Bootstrap units are scheduled five-session evaluation dates, not daily labels.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from qwb import daily, daily_data, experiments
from qwb import governance as g
from qwb.bench.statistics import block_summary

import numpy as np
import pandas as pd


def evaluate(args):
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=False)
    record = {"scope": "risk_forecast_only_historical", "status": "RUNNING",
              "arguments": vars(args), "created_at": experiments.now()}
    experiments.write(root / "result.json", record)
    try:
        snapshot = daily_data.current(args.data_dir)
        panel = daily.load_panel(snapshot)
        source = Path(args.predictions)
        frame = (pd.read_parquet(source) if source.suffix.lower() == ".parquet"
                 else pd.read_csv(source, dtype={"code": str}))
        snapshot_id = experiments.read(snapshot / "snapshot.json")["data_snapshot_id"]
        if "data_snapshot_id" in frame and set(frame.data_snapshot_id) != {snapshot_id}:
            raise ValueError("预测与行情快照不一致")
        dates = panel["close"].index
        calendar = pd.read_csv(snapshot / "calendar.csv", dtype=str)
        open_days = pd.DatetimeIndex(pd.to_datetime(calendar.loc[calendar.is_open.eq("1"), "date"]))
        if not dates.equals(open_days[(open_days >= dates.min()) & (open_days <= dates.max())]):
            raise ValueError("行情与交易日历不连续")
        start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
        if start < dates.min() or end > dates.max() or start > end:
            raise ValueError("评估区间超出行情覆盖或顺序错误")
        selected = dates[(dates >= start) & (dates <= end)]
        if len(selected) < 2:
            raise ValueError("评估至少需要两个交易日")
        signals = g.signal_matrix(frame, panel)
        cfg = g.GovernanceConfig(n_hold=args.n_hold, hold_until_rank=args.n_hold)
        risk = g.RiskData(panel)
        samples = g.risk_samples(risk, signals, args.n_hold)
        head = g.RiskHead(samples)
        rows = []
        for day in selected[::5]:
            row = {"date": str(day.date()), "status": "NO_MATURE_LABEL"}
            if day not in signals.index:
                raise ValueError(f"{day:%Y-%m-%d} 缺少已排期的信号，不能跳过")
            basket, _ = g.choose(signals.loc[day], set(), cfg)
            features, label = risk.features(day, basket), risk.label(day, basket)
            if features is None:
                row["status"] = "INSUFFICIENT_PRICE_HISTORY"
            else:
                ml, reason = head.predict(day, dates.get_loc(day), *features)
                row.update(basket=json.dumps(basket), ewma_variance=features[1],
                           ml_variance=ml, risk_reason=reason,
                           fit_day=head.model["fit_day"] if head.model else None,
                           max_train_label_end=head.model["max_label_end"] if head.model else None)
                if label is not None and label[1] <= selected[-1]:
                    def loss(forecast):
                        ratio = label[0] / forecast
                        return float(ratio - np.log(ratio) - 1)
                    row.update(status="SCORED", label_end=str(label[1].date()),
                               target_variance=label[0], ewma_qlike=loss(features[1]), ml_qlike=loss(ml))
                    if reason == "ML_RISK":
                        row["paired_ml_minus_ewma"] = row["ml_qlike"] - row["ewma_qlike"]
            rows.append(row)
        scores = pd.DataFrame(rows)
        delta = scores.get("paired_ml_minus_ewma", pd.Series(np.nan, index=scores.index))
        uncertainty = block_summary(delta.to_numpy(float), block_days=4, resamples=2000,
                                    seed=20261023, min_days=40)[0]
        paired = scores.loc[delta.notna()]
        scores.to_csv(root / "scores.csv", index=False)
        samples.to_parquet(root / "risk_samples.parquet", index=False)
        (root / "risk_models.jsonl").write_text("".join(json.dumps(m)+"\n" for m in head.checkpoints), encoding="utf-8")
        record.update(status="SUCCEEDED", scheduled_dates=len(scores), mature_ml_pairs=len(paired),
            warmup_scored_dates=sum(r.get("status") == "SCORED" and r.get("risk_reason") == "ML_WARMUP_EWMA" for r in rows),
            mean_ewma_qlike=float(paired.ewma_qlike.mean()) if len(paired) else None,
            mean_ml_qlike=float(paired.ml_qlike.mean()) if len(paired) else None,
            paired_ml_minus_ewma=uncertainty,
            bootstrap={"unit": "scheduled_5_session_evaluation_row", "block_rows": 4,
                       "resamples": 2000, "seed": 20261023, "min_valid_rows": 40,
                       "interval": "descriptive_paired_circular_block_percentile_95"},
            risk_spec={"features": g.FEATURES, "horizon": g.HORIZON, "min_train": g.MIN_TRAIN,
                       "ridge_alpha": g.RIDGE_ALPHA, "refit_sessions": g.REFIT_SESSIONS,
                       "basket": "common_decision_known_top_n_equal_weight",
                       "label": "annualized_mean_square_adjusted_open_returns"},
            snapshot_id=snapshot_id, signal_hash=experiments.digest(source),
            data_files={p.relative_to(snapshot).as_posix(): experiments.digest(p)
                        for p in sorted(snapshot.rglob("*")) if p.is_file() and p.suffix in {".parquet", ".csv", ".json"}},
            implementation_hash=experiments.object_digest({"diagnostic": experiments.digest(Path(__file__)),
                "governance": experiments.digest(Path(g.__file__)),
                "daily": experiments.digest(Path(daily.__file__))}),
            environment={"numpy": np.__version__, "pandas": pd.__version__},
            limitations=["风险预测检验，不产生可成交净值，不处理股数账户公司行动。",
                         "复权收益代理不是高频 realized variance；数据不是完整市场。",
                         "上游信号与历史数据是否当时可用仍取决于来源记录，导入不构成审计认证。",
                         "仅比较真正已拟合 ML 的共同日期；暖机、无特征、未到期日期保留。",
                         "块区间依赖样本与相关结构，不是盈利证明；设置在查看结果前固定。"])
    except BaseException as exc:
        record.update(status="FAILED", error=str(exc))
        experiments.write(root / "result.json", record)
        raise
    experiments.write(root / "result.json", record)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("predictions", "data-dir", "start", "end", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--n-hold", type=int, default=10)
    result = evaluate(parser.parse_args())
    print(json.dumps({k: result[k] for k in ("status", "scheduled_dates", "mature_ml_pairs",
        "mean_ewma_qlike", "mean_ml_qlike", "paired_ml_minus_ewma")}, ensure_ascii=False, indent=2))
