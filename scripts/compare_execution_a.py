"""Same saved out-of-fold predictions, sequential execution-assumption changes."""
from pathlib import Path
import argparse
import base64
import json
import re
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qwb import daily, dataset, experiments, netcost, pipeline, strategy

import numpy as np
import pandas as pd
import yaml

REFERENCE = "examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml"
REFERENCE_URL = f"https://github.com/microsoft/qlib/blob/v0.9.7/{REFERENCE}"


def reference_bytes(path=None):
    if path:
        return Path(path).read_bytes()
    url = f"https://api.github.com/repos/microsoft/qlib/contents/{REFERENCE}?ref=v0.9.7"
    request = urllib.request.Request(url, headers={"User-Agent": "quant-workbench-execution-comparison"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return base64.b64decode(json.load(response)["content"])


def run(run_dir, data_dir, out, reference=None):
    run_dir, data_dir, out = Path(run_dir), Path(data_dir), Path(out)
    if out.exists():
        raise ValueError("Use a new output directory; comparison artifacts cannot be overwritten")
    raw_reference = reference_bytes(reference)
    ref = yaml.safe_load(raw_reference)
    exchange = ref["port_analysis_config"]["backtest"]["exchange_kwargs"]
    if any(exchange[k] != v for k, v in {"deal_price": "close", "limit_threshold": .095,
            "open_cost": .0005, "close_cost": .0015, "min_cost": 5}.items()):
        raise ValueError("Qlib reference parameters differ from the pinned comparison")
    config = yaml.safe_load((run_dir / "config.resolved.yaml").read_text("utf-8"))
    card = yaml.safe_load((run_dir / "task.yaml").read_text("utf-8"))
    cfg = config["cfg"]
    protocol, protocol_hash = strategy.protocol()
    lock_path = ROOT / "bench/protocols/daily_strategy_v1.lock.json"
    if experiments.read(lock_path)["protocol_hash"] != protocol_hash:
        raise ValueError("Protocol differs from its previously frozen lock")
    if any(card["label"][edge].get(field) != protocol["label"][edge][field]
           for edge in ("start", "end") for field in ("day", "price")):
        raise ValueError("Model task differs from the frozen open-to-open comparison label")
    if (cfg["universe"] != "all_ex_st" or cfg["min_age_days"] != 60 or
            cfg["min_amount_quantile"] != .05 or cfg["portfolio"].get("n_hold") or
            cfg["portfolio"]["top"] != .1 or cfg["portfolio"]["commission_bp"] != 3):
        raise ValueError("Run is outside the frozen default comparison group")
    print("loading original research snapshot", flush=True)
    P, snapshot_id, st, members = pipeline.load_data({"data": {"source": "snapshot", "dir": str(data_dir)},
                                                     "cfg": cfg}, ROOT)
    dates, codes = P["close"].index, P["close"].columns
    predictions = pd.read_parquet(run_dir / "predictions.parquet")
    if set(predictions.data_snapshot_id) != {snapshot_id}:
        raise ValueError("Saved prediction snapshot does not match evaluation data")
    predictions["date"] = pd.to_datetime(predictions.decision_time, utc=True).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    if predictions.duplicated(["date", "code"]).any():
        raise ValueError("Duplicate saved predictions")
    signal = predictions.pivot(index="date", columns="code", values="prediction").reindex(index=dates, columns=codes).to_numpy()
    eligible = daily.universe(P, cfg, st, members).to_numpy()
    supported = np.array([bool(re.fullmatch(r"60\d{4}\.SH|68[89]\d{3}\.SH|00\d{4}\.SZ|30\d{4}\.SZ", c)) for c in codes])
    y_open, y_close = daily.labels(P, card).to_numpy(), netcost.labels_close(P, card).to_numpy()
    # All variants select from the identical observed stock-days, including price-label coverage.
    common = eligible & supported[None, :] & np.isfinite(signal) & np.isfinite(y_open) & np.isfinite(y_close)
    years = cfg["final_test_years"]
    print("computing lagged STRUCT spread and four execution variants", flush=True)
    spread = netcost.struct_half_spread_bp(P)
    variants = [
        ("A0", "开盘 / 分板涨跌停 / STRUCT", y_open, {}),
        ("A1", "收盘 / 分板涨跌停 / STRUCT", y_close, {"fill": "close"}),
        ("A2", "收盘 / 统一 9.5% / STRUCT", y_close, {"fill": "close", "uniform_limit": .095}),
        ("A3", "收盘 / 统一 9.5% / 固定 5/15 bp", y_close, {"fill": "close", **netcost.QLIB_STYLE}),
    ]
    rows = []
    baseline = None
    for key, name, label, options in variants:
        value = netcost.evaluate(P, card, {"same_prediction": signal}, common, label, years,
            top=.1, commission_bp=3, half_spread=spread, **options)
        result = value["signals"]["same_prediction"]
        if baseline is None:
            baseline = value["benchmark_ann"]
        rows.append(dict(id=key, convention=name, rebalances=value["rebalances"],
                         own_benchmark_ann=value["benchmark_ann"], **result,
                         common_open_benchmark_net_excess_ann=result["gross_ann"]-result["cost_ann"]-baseline))
    out.mkdir(parents=True)
    (out / "qlib-reference.yaml").write_bytes(raw_reference)
    identity = {"protocol_hash": protocol_hash, "snapshot_id": snapshot_id,
        "protocol_lock_hash": experiments.digest(lock_path),
        "data": dataset.fingerprint(data_dir),
        "run_files": {n: experiments.digest(run_dir / n) for n in
                      ["predictions.parquet", "folds.json", "config.resolved.yaml", "task.yaml", "acceptance.lock"]},
        "evaluator_hash": experiments.digest(Path(netcost.__file__)),
        "script_hash": experiments.digest(Path(__file__)),
        "qlib_reference_hash": experiments.digest(out / "qlib-reference.yaml"),
        "qlib_reference_url": REFERENCE_URL, "years": years,
        "evaluation": "same_saved_oof_predictions_common_eligible_stock_days_supported_boards",
        "cost_scope": "weight_proxy_STRUCT_commission_stamp_no_minimum_fee_no_share_lots_no_impact",
        "engine": "qwb_netcost_adapter_not_qlib_engine", "retrained": False}
    manifest = {"created_at": experiments.now(), "identity": identity, "identity_hash": experiments.object_digest(identity),
        "rows": rows, "outcome": "execution_sensitivity_only_not_platform_superiority",
        "sample_warning": "2024-2025 have previously been inspected; this is not a new sealed test"}
    experiments.write(out / "result.json", manifest)
    pd.DataFrame(rows).to_csv(out / "comparison.csv", index=False, encoding="utf-8-sig")
    lines = ["# 执行实验 A：同一预测，改变执行口径", "",
        "本次不重训，复用已有逐年样本外预测；2024–2025 已被多次查看，只作开发对照。", "",
        "| 口径 | 毛收益/年 | 成本/年 | 净超额/年（同一开盘基准） | 相邻变化 |",
        "| --- | ---: | ---: | ---: | ---: |"]
    previous = None
    for row in rows:
        excess = row["common_open_benchmark_net_excess_ann"]
        delta = "—" if previous is None else f"{100*(excess-previous):+.2f} pp"
        lines.append(f"| {row['id']} {row['convention']} | {row['gross_ann']:.2%} | {row['cost_ann']:.2%} | {excess:+.2%} | {delta} |")
        previous = excess
    lines += ["", "## 能说明什么", "",
        "同一预测的收益会随成交价、涨跌停规则和费用口径改变。统一开盘等权股票池基准用于上表；CSV 另保留各口径自己的基准及净超额，避免基准变化混入归因。年化为每期均值乘 252/5，不是账户复合收益率。", "",
        "## 边界", "",
        "- 这是 QWB 权重代理评估器的执行假设敏感性实验，没有调用 Qlib 引擎，也没有训练 Qlib Alpha158 模型；不能据此宣称超过 Qlib。",
        "- 仅适配官方示例的成交价、阈值与费率。官方示例另外采用 CSI300、TopkDropout、1 亿元账户及最低 5 元费用，这些没有被冒充复现。",
        "- 冻结的 5 日任务、前 10% 等权、时点 ST/上市年龄/流动性过滤，以及受支持沪深板块在四组间一致。共同可用的开盘和收盘标签也固定。",
        "- 当前评估器不是整股账户账本：不含最低佣金、整手、价格冲击与完整公司行动现金流；持仓收益缺失按代理规则处理，受阻持仓的权重也为近似。不能替代步骤 2 的前向账户或认证可实现收益。",
        "- Qlib 支持自定义成交价、涨跌停表达式、停牌、最低费用和数量规则。此处比较的是选定示例参数，不是引擎能力上限。",
        "- 原研究预测曾转为 float32 保存，本次各组均复用相同文件；新冻结的受支持板块/共同标签口径与旧报告并非完全相同，旧数字不覆盖。", "",
        f"官方配置：[Qlib v0.9.7]({REFERENCE_URL})；本地副本及哈希见 result.json。", "",
        f"协议：`{protocol_hash}`。输入、脚本和结果身份：`{manifest['identity_hash']}`。", "",
        "下一项仍是步骤 3 的训练实验 B：共同数据、共同评估协议下比较训练配方。STRUCT 接入整股账本另外交付，已有前向账户不静默改费用。", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"output": str(out), "identity_hash": manifest["identity_hash"], "rows": rows}, ensure_ascii=False), flush=True)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--run", default="runs/cn_rank_5d_lgbm_v2")
    parser.add_argument("--data", required=True, help="original prediction snapshot, not the live append feed")
    parser.add_argument("--out", default="artifacts/execution-a-20261023")
    parser.add_argument("--qlib-reference", help="local pinned configuration for offline reproduction")
    args = parser.parse_args()
    run(args.run, args.data, args.out, args.qlib_reference)
