"""Bounded, development-only B0--B5 fits; never promotes a model or opens an account."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import time

for _key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_key] = "2"

import torch
from torch import nn
from qwb import daily, netcost
from qwb.bench import account
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
HORIZONS = (5, 10, 20)
MIXES = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [1/3, 1/3, 1/3]])


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


def feature_frames(p):
    f = daily.raw_features(p)
    ac = p["close"] * p["adj_factor"]
    r = ac.pct_change(fill_method=None)
    for n in (2, 3, 10, 40, 120):
        f[f"r{n}"] = ac / ac.shift(n) - 1
    for n in (5, 10, 60):
        f[f"vol{n}"] = r.rolling(n).std()
    for n in (5, 10, 20, 60):
        f[f"ma{n}"] = ac / ac.rolling(n).mean() - 1
    for n in (5, 60):
        f[f"amt{n}"] = p["amount"] / p["amount"].rolling(n).mean()
    f["amt_trend"] = p["amount"].rolling(5).mean() / p["amount"].rolling(20).mean()
    width = (p["high"] - p["low"]).replace(0, np.nan)
    f["close_pos"] = (p["close"] - p["low"]) / width
    f["body"] = (p["close"] - p["open"]) / p["close"]
    f["upper_wick"] = (p["high"] - np.maximum(p["open"], p["close"])) / width
    f["lower_wick"] = (np.minimum(p["open"], p["close"]) - p["low"]) / width
    f["downside20"] = r.clip(upper=0).rolling(20).std()
    f["high_distance"] = ac / ac.rolling(20).max() - 1
    f["low_distance"] = ac / ac.rolling(20).min() - 1
    assert len(f) == 32
    return f


def load_data():
    source = ROOT / "artifacts/model-quality-20261022/sample"
    files = [source / "bars" / f"{year}.parquet" for year in range(2018, 2024)]
    # Explicit partitions: never load live bars or any 2026 outcomes.
    bars = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    bars["date"] = pd.to_datetime(bars.date)
    if not bars.date.between("2018-01-01", "2023-12-31").all():
        raise ValueError("development data outside registered range")
    p = daily.panel_from_bars(bars)
    dates, codes = p["close"].index, p["close"].columns
    status = pd.read_parquet(source / "st_status.parquet")
    st = daily.st_wide(status, dates, codes)
    u = daily.universe(p, {"min_age_days": 60, "min_amount_quantile": .05,
                           "universe": "all_ex_st"}, st=st).to_numpy().copy()
    raw = feature_frames(p)
    x = np.stack([v.where(np.isfinite(v)).rank(axis=1, pct=True).to_numpy() - .5
                  for v in raw.values()], axis=-1)
    ac = p["close"] * p["adj_factor"]
    r1 = ac.pct_change(fill_method=None)
    summaries = pd.DataFrame({
        "market_r1": r1.where(u).mean(axis=1),
        "market_r5": raw["r5"].where(u).mean(axis=1),
        "market_r20": raw["r20"].where(u).mean(axis=1),
        "market_vol": raw["vol20"].where(u).median(axis=1),
        "market_log_amount": raw["amt20"].where(u).mean(axis=1),
        "market_breadth": r1.where(u).gt(0).sum(axis=1) / u.sum(axis=1).clip(1),
    }).fillna(0)
    # Fixed physical scales, not full-history fitted normalization.
    summaries = summaries.to_numpy() / np.array([.02, .05, .1, .03, 20, 1])
    x = np.concatenate([x, np.broadcast_to(summaries[:, None], (*x.shape[:2], 6))], axis=-1)
    u &= np.isfinite(x).all(axis=-1)
    x = np.nan_to_num(x).astype("float32")
    ao = p["open"] * p["adj_factor"]
    ys = [ao.shift(-(h+1)) / ao.shift(-1) - 1 for h in HORIZONS]
    yraw = np.stack([y.to_numpy() for y in ys], axis=-1)
    yrank = np.stack([y.where(u).rank(axis=1, pct=True).to_numpy() - .5 for y in ys], axis=-1)
    labelmask = u & np.isfinite(yraw).all(axis=-1)
    spread = netcost.struct_half_spread_bp(p).to_numpy()
    normal = bars.pivot(index="date", columns="code", values="state").reindex(index=dates, columns=codes)
    normal = normal.eq("normal").to_numpy()
    # Open t+1 to open t+2, used once in each chronological utility episode.
    rr = (ao.shift(-2) / ao.shift(-1) - 1).to_numpy()
    return {"p": p, "x": x, "u": u, "yrank": np.nan_to_num(yrank).astype("float32"),
            "yraw": yraw, "labelmask": labelmask, "spread": spread, "normal": normal,
            "rr": np.nan_to_num(rr).astype("float32"), "dates": dates, "codes": codes,
            "features": list(raw) + ["market_r1", "market_r5", "market_r20", "market_vol",
                                      "market_log_amount", "market_breadth"],
            "hashes": {path.relative_to(ROOT).as_posix(): digest(path)
                       for path in files + [source / "st_status.parquet", source / "snapshot.json"]}}


def split_dates(dates, year):
    outer = int(np.flatnonzero(dates.year == year)[0])
    mature = np.arange(120, outer - 21)
    val = mature[-60:]
    train = mature[mature + 21 < val[0]]
    assert train[-1] + 21 < val[0] and val[-1] + 21 < outer
    return train, val, mature, np.flatnonzero(dates.year == year)


def daily_ic(pred, truth, mask):
    a = pd.DataFrame(pred).where(mask).rank(axis=1)
    b = pd.DataFrame(truth).where(mask).rank(axis=1)
    return a.corrwith(b, axis=1).to_numpy()


class Net(nn.Module):
    def __init__(self, features, context):
        super().__init__()
        self.context = context
        self.encoder = nn.Sequential(nn.Linear(features, 64), nn.GELU(), nn.Linear(64, 64), nn.GELU())
        if context:
            self.tokens = nn.Parameter(torch.randn(8, 64) * .02)
            self.pool = nn.MultiheadAttention(64, 4, batch_first=True)
            self.back = nn.MultiheadAttention(64, 4, batch_first=True)
        self.head = nn.Sequential(nn.Linear(128 if context else 64, 64), nn.GELU(), nn.Linear(64, 3))

    def forward(self, x, valid):
        z = self.encoder(x)
        if self.context:
            q = self.tokens[None].expand(len(x), -1, -1)
            pooled, _ = self.pool(q, z, z, key_padding_mask=~valid, need_weights=False)
            back, _ = self.back(z, pooled, pooled, need_weights=False)
            z = torch.cat([z, back], dim=-1)
        return self.head(z)


def tensors(d):
    return torch.from_numpy(d["x"]), torch.from_numpy(d["u"]), torch.from_numpy(d["yrank"]), torch.from_numpy(d["labelmask"])


def predict(net, tx, tu, days):
    net.eval()
    with torch.no_grad():
        return torch.cat([net(tx[batch], tu[batch]) for batch in np.array_split(days, max(1, int(np.ceil(len(days)/32))))]).numpy()


def pretrain(d, train, val, full, seed, context, out):
    tx, tu, ty, tm = tensors(d)
    def build():
        torch.manual_seed(seed)
        return Net(tx.shape[-1], context)
    def epoch(net, optimizer, days, rng):
        net.train()
        order = rng.permutation(days)
        losses = []
        for batch in np.array_split(order, max(1, int(np.ceil(len(order)/16)))):
            output = net(tx[batch], tu[batch])
            loss = ((output - ty[batch]) ** 2)[tm[batch]].mean()
            optimizer.zero_grad(); loss.backward(); nn.utils.clip_grad_norm_(net.parameters(), 1.)
            optimizer.step(); losses.append(float(loss.detach()))
        return float(np.mean(losses))
    net = build()
    opt = torch.optim.AdamW(net.parameters(), lr=.001, weight_decay=.001)
    rng = np.random.default_rng(seed)
    best, best_epoch, best_state, history = -np.inf, 0, None, []
    for ep in range(1, 26):
        loss = epoch(net, opt, train, rng)
        pred = predict(net, tx, tu, val)
        ic = float(np.nanmean(daily_ic(pred[..., 0], d["yraw"][val, :, 0], d["labelmask"][val])))
        history.append({"epoch": ep, "loss": loss, "validation_ic5": ic})
        if ic > best + 1e-7:
            best, best_epoch, best_state = ic, ep, copy.deepcopy(net.state_dict())
        if ep - best_epoch >= 5:
            break
    net.load_state_dict(best_state)
    inner = copy.deepcopy(net)
    vp = predict(inner, tx, tu, val)
    net = build()
    opt = torch.optim.AdamW(net.parameters(), lr=.001, weight_decay=.001)
    rng = np.random.default_rng(seed)
    for _ in range(best_epoch):
        epoch(net, opt, full, rng)
    torch.save(net.state_dict(), out / "prediction-checkpoint.pt")
    write(out / "prediction-selection.json", {"epochs": best_epoch, "history": history,
          "parameters": sum(p.numel() for p in net.parameters())})
    return net, inner, vp


def train_trees(d, train, val, full, test, seed, out):
    import lightgbm as lgb
    mask = d["labelmask"]
    predictions = np.zeros((len(test), len(d["codes"]), 3), dtype="float32")
    validation = np.zeros((len(val), len(d["codes"]), 3), dtype="float32")
    for hi, h in enumerate(HORIZONS):
        xt = d["x"][train][mask[train]]; yt = d["yrank"][train, :, hi][mask[train]]
        xv = d["x"][val][mask[val]]; yv = d["yrank"][val, :, hi][mask[val]]
        positions = np.flatnonzero(mask[val].ravel())
        def feval(pred, _data):
            a = np.full(mask[val].shape, np.nan); a.ravel()[positions] = pred
            return "daily_rank_ic", float(np.nanmean(daily_ic(a, d["yraw"][val, :, hi], mask[val]))), True
        best = None; history = []
        for leaves, minleaf in ((63, 1000), (31, 500)):
            params = dict(objective="regression", metric="None", learning_rate=.05, num_leaves=leaves,
                          min_data_in_leaf=minleaf, feature_fraction=.8, bagging_fraction=.8,
                          bagging_freq=1, lambda_l2=1., num_threads=2, deterministic=True,
                          force_row_wise=True, verbosity=-1, seed=seed)
            model = lgb.train(params, lgb.Dataset(xt, label=yt), num_boost_round=500,
                              valid_sets=[lgb.Dataset(xv, label=yv)], feval=feval,
                              callbacks=[lgb.early_stopping(30, first_metric_only=False, verbose=False)])
            score = float(np.nanmean(daily_ic(model.predict(d["x"][val].reshape(-1, xt.shape[-1])).reshape(mask[val].shape),
                                           d["yraw"][val, :, hi], mask[val])))
            history.append({"leaves": leaves, "min_leaf": minleaf, "rounds": model.best_iteration, "validation_ic": score})
            if best is None or score > best[0]:
                best = score, params, model.best_iteration, model
        score, params, rounds, inner = best
        validation[..., hi] = inner.predict(d["x"][val].reshape(-1, xt.shape[-1])).reshape(mask[val].shape)
        fitted = lgb.train(params, lgb.Dataset(d["x"][full][mask[full]], label=d["yrank"][full, :, hi][mask[full]]),
                           num_boost_round=max(1, rounds))
        fitted.save_model(str(out / f"tree-{h}.txt"))
        predictions[..., hi] = fitted.predict(d["x"][test].reshape(-1, xt.shape[-1])).reshape(mask[test].shape)
        write(out / f"selection-{h}.json", {"selected_rounds": rounds, "history": history})
        log(f"tree horizon={h} rounds={rounds} valIC={score:.4f}")
    return predictions, validation


def calibration(vp, raw, mask):
    slopes = []
    for h in range(3):
        a = pd.DataFrame(vp[..., h]).where(mask)
        b = pd.DataFrame(raw[..., h]).where(mask)
        av = a.sub(a.mean(axis=1), axis=0).to_numpy()[mask]
        bv = b.sub(b.mean(axis=1), axis=0).to_numpy()[mask]
        slopes.append(max(0., float(np.dot(av, bv) / max(np.dot(av, av), 1e-12))))
    forecast = vp * np.array(slopes) / np.array(HORIZONS)
    scores = [float(np.nanmean(daily_ic(forecast @ mix, raw[..., 0], mask)))
              if np.std(forecast @ mix) > 1e-15 else -1. for mix in MIXES]
    choice = int(np.argmax(scores))
    return {"slopes": slopes, "mix": MIXES[choice].tolist(), "validation_ic5_by_mix": scores}


def calibrated(pred, cal):
    return (pred * np.array(cal["slopes"]) / np.array(HORIZONS)) @ np.array(cal["mix"])


def torch_weights(score, mask, previous, spread):
    # Fixed 95% exposure. Top-k is piecewise differentiable, not a QP solver.
    cost = (.0003 + spread / 10000 + .0005) * (previous <= 1e-8)
    value = score * 5 - cost
    value = value.masked_fill(~mask, -1e9)
    k = min(20, int(mask.sum()))
    if k < 10:
        raise ValueError("not enough eligible names")
    ids = torch.topk(value, k).indices
    scale = value[mask].std(unbiased=False).detach().clamp_min(1e-5) * .2
    weights = torch.softmax(value[ids] / scale, dim=0)
    # Water filling: cap while preserving differentiability in the free set.
    fixed = torch.zeros_like(weights, dtype=torch.bool)
    for _ in range(k):
        free = ~fixed
        alloc = .95 - fixed.sum() * .1
        proposed = weights * free / (weights * free).sum().clamp_min(1e-20) * alloc
        newly = (proposed > .1) & free
        if not bool(newly.any()):
            weights = torch.where(fixed, torch.full_like(weights, .1), proposed)
            break
        fixed = fixed | newly
    return torch.zeros_like(score).scatter(0, ids, weights)


def utility_episode(net, d, tx, tu, ty, tm, days, cal):
    pred = net(tx[days], tu[days])
    factor = torch.tensor(np.array(cal["slopes"]) / np.array(HORIZONS) * np.array(cal["mix"]), dtype=torch.float32)
    scores = pred @ factor
    rr = torch.from_numpy(d["rr"][days])
    spread = torch.tensor(np.nan_to_num(d["spread"][days], nan=100), dtype=torch.float32)
    prev = torch.zeros(tx.shape[1]); daily_returns = []; weights_log = []
    for j in range(len(days)):
        fee = torch.tensor(0.)
        if j % 5 == 0:
            w = torch_weights(scores[j], tu[days[j]], prev, spread[j])
            delta = w - prev
            # Smooth proportional proxy; exact minimum fees/lots are not claimed.
            fee = (delta.abs() * (.0003 + spread[j] / 10000) + (-delta).clamp_min(0) * .001).sum()
        else:
            w = prev
        gross = (w * rr[j]).sum()
        daily_returns.append(gross - fee)
        weights_log.append(w)
        prev = w * (1 + rr[j]) / (1 + gross).clamp_min(.1)
    rets = torch.stack(daily_returns)
    aux = ((pred - ty[days]) ** 2)[tm[days]].mean()
    loss = -torch.log1p(rets.clamp_min(-.9)).mean() + 5 * rets.var(unbiased=False) + .01 * aux
    return loss, rets, torch.stack(weights_log)


def finetune(net, inner, d, train, val, full, cal, out):
    tx, tu, ty, tm = tensors(d)
    def run_epoch(model, days, opt):
        model.train(); logs = []
        for start in range(0, len(days)-19, 20):
            batch = days[start:start+20]
            loss, _, _ = utility_episode(model, d, tx, tu, ty, tm, batch, cal)
            opt.zero_grad(); loss.backward()
            grad = float(torch.sqrt(sum((p.grad ** 2).sum() for p in model.parameters() if p.grad is not None)))
            nn.utils.clip_grad_norm_(model.parameters(), 1.); opt.step()
            logs.append({"loss": float(loss.detach()), "gradient_l2": grad})
        return logs
    def validation(model):
        model.eval()
        returns = []
        with torch.no_grad():
            for start in range(0, len(val)-19, 20):
                _, r, _ = utility_episode(model, d, tx, tu, ty, tm, val[start:start+20], cal)
                returns.append(r)
        r = torch.cat(returns)
        return float(torch.log1p(r).mean() - 5*r.var(unbiased=False))
    best_value, best_epoch = validation(inner), 0
    history = [{"epoch": 0, "validation_utility": best_value}]
    opt = torch.optim.AdamW(inner.parameters(), lr=.0001, weight_decay=.001)
    for ep in range(1, 6):
        grads = run_epoch(inner, train, opt)
        value = validation(inner)
        history.append({"epoch": ep, "validation_utility": value, "train": grads})
        if value > best_value + 1e-10:
            best_value, best_epoch = value, ep
    before = copy.deepcopy(net.state_dict())
    with torch.no_grad():
        _, _, w0 = utility_episode(net, d, tx, tu, ty, tm, full[:20], cal)
    opt = torch.optim.AdamW(net.parameters(), lr=.0001, weight_decay=.001)
    for _ in range(best_epoch):
        run_epoch(net, full, opt)
    with torch.no_grad():
        _, _, w1 = utility_episode(net, d, tx, tu, ty, tm, full[:20], cal)
    write(out / "utility-selection.json", {"selected_epochs": best_epoch, "history": history,
          "parameter_change_l2": float(torch.sqrt(sum(((v-before[k])**2).sum() for k,v in net.state_dict().items()))),
          "training_action_l1_change": float((w1-w0).abs().sum()),
          "qualification": "exploratory_piecewise_head_not_exact_account_qualified"})
    torch.save(net.state_dict(), out / "utility-checkpoint.pt")
    return net


def targets_for(d, score, test, standard=False):
    previous = torch.zeros(len(d["codes"])); targets = {}
    for t in test[::5]:
        valid = d["u"][t] & np.isfinite(score[t]) & np.isfinite(d["spread"][t])
        if standard:
            ids = np.flatnonzero(valid)[np.argsort(-score[t, valid], kind="stable")[:20]]
            w = np.zeros(len(previous)); w[ids] = .95 / len(ids)
        else:
            with torch.no_grad():
                w = torch_weights(torch.tensor(np.nan_to_num(score[t]), dtype=torch.float32),
                                  torch.from_numpy(valid), previous,
                                  torch.tensor(np.nan_to_num(d["spread"][t], nan=100), dtype=torch.float32)).numpy()
        w = np.minimum(w.astype("float64"), .1)
        if w.sum() > .95:
            w *= .95 / w.sum()
        previous = torch.from_numpy(w.copy()).float()
        targets[int(t)] = w
    return targets


def evaluate(d, pred, cal, test, arm, seed, year, out):
    raw_score = pred[..., 0] if arm == "B0" else calibrated(pred, cal)
    score = np.full(d["u"].shape, np.nan); score[test] = raw_score
    targets = targets_for(d, score, test, standard=arm == "B0")
    write(out / f"{arm}-targets.json", {str(d["dates"][t].date()): dict(zip(d["codes"][w>0], w[w>0].tolist()))
                                      for t, w in targets.items()})
    for slip in (0., 10.):
        path, fills, holdings = account.replay(d["p"], d["normal"], d["spread"], targets,
            start=int(test[0]), end=int(test[-1]), initial_cash=1e6, commission_bp=3., extra_slippage_bp=slip)
        name = f"{arm}-slip{int(slip)}"
        path.assign(arm=arm, seed=seed, year=year, stress_bp=slip).to_parquet(out / f"{name}-path.parquet", index=False)
        fills.to_parquet(out / f"{name}-fills.parquet", index=False)
        if slip == 0:
            holdings.to_parquet(out / f"{name}-holdings.parquet", index=False)
        metrics = account.metrics(path, 1e6)
        r = path.net_return.to_numpy()[1:]
        metrics.update(utility_ann=float(252*(np.log1p(r).mean() - 5*np.var(r))),
                       mean_exposure=float((path.holdings_value / path.equity).iloc[1:].mean()))
        write(out / f"{name}-metrics.json", metrics)
    # Prediction evidence uses common mature-label rows, not future eligibility.
    ic = {str(h): float(np.nanmean(daily_ic(pred[..., j], d["yraw"][test, :, j], d["labelmask"][test])))
          for j, h in enumerate(HORIZONS)}
    write(out / f"{arm}-ic.json", ic)
    return metrics


def run_case(d, year, seed, out):
    out.mkdir(parents=True, exist_ok=False)
    started, cpu = time.perf_counter(), time.process_time()
    train, val, full, test = split_dates(d["dates"], year)
    write(out / "fold.json", {"year": year, "seed": seed,
          "train_last": d["dates"][train[-1]], "validation_first": d["dates"][val[0]],
          "validation_last_label_end": d["dates"][val[-1]+21],
          "refit_last_label_end": d["dates"][full[-1]+21], "outer_start": d["dates"][test[0]],
          "training_dates": len(train), "refit_dates": len(full)})
    predictions = {}
    tree_out = out / "trees"; tree_out.mkdir()
    pred, vp = train_trees(d, train, val, full, test, seed, tree_out)
    cal = calibration(vp, d["yraw"][val], d["labelmask"][val])
    b0cal = {**cal, "mix": [1, 0, 0]}
    for arm, this_cal in (("B0", b0cal), ("B0-cost", b0cal), ("B1", cal)):
        evaluate(d, pred, this_cal, test, arm, seed, year, out)
        predictions[arm] = pred.copy()
        write(out / f"{arm}-calibration.json", this_cal)
    tx, tu, _, _ = tensors(d)
    for context, base, tuned in ((False, "B2", "B3"), (True, "B4", "B5")):
        net_out = out / base; net_out.mkdir()
        net, inner, vp = pretrain(d, train, val, full, seed, context, net_out)
        cal = calibration(vp, d["yraw"][val], d["labelmask"][val])
        pred = predict(net, tx, tu, test)
        predictions[base] = pred.copy()
        evaluate(d, pred, cal, test, base, seed, year, out)
        write(out / f"{base}-calibration.json", cal)
        log(f"{year} seed={seed} {base} pretrained")
        tuned_out = out / tuned; tuned_out.mkdir()
        net = finetune(net, inner, d, train, val, full, cal, tuned_out)
        pred = predict(net, tx, tu, test)
        predictions[tuned] = pred.copy()
        evaluate(d, pred, cal, test, tuned, seed, year, out)
        write(out / f"{tuned}-calibration.json", cal)
        log(f"{year} seed={seed} {tuned} tuned")
    np.savez_compressed(out / "predictions.npz", dates=d["dates"][test].to_numpy(),
                        codes=d["codes"].to_numpy(dtype=str), **predictions)
    write(out / "completed.json", {"wall_seconds": time.perf_counter()-started,
                                   "cpu_seconds": time.process_time()-cpu, "state": "succeeded"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--year", type=int, choices=(2021, 2022, 2023))
    parser.add_argument("--seed", type=int, choices=(0, 1, 2))
    args = parser.parse_args()
    torch.set_num_threads(2)
    args.output.mkdir(parents=True, exist_ok=True)
    registration = args.output / "registration.json"
    if not registration.exists():
        write(registration, {"protocol_sha256": digest(ROOT / "docs/research/TRAINING_COMPARISON_PROTOCOL_20261026.md"),
              "script_sha256": digest(Path(__file__)), "source_hashes": {str(p.relative_to(ROOT)): digest(p)
              for p in (ROOT / "src/qwb").rglob("*.py")}, "status": "frozen_before_fit",
              "registered_at": pd.Timestamp.now(tz="UTC")})
    frozen = json.loads(registration.read_text())
    if frozen["script_sha256"] != digest(Path(__file__)):
        raise ValueError("runner changed after registration; use a new attempt directory")
    d = load_data()
    write(args.output / "data.json", {"hashes": d["hashes"], "features": d["features"],
          "dates": len(d["dates"]), "codes": len(d["codes"]), "eligible_rows": int(d["u"].sum()),
          "torch": torch.__version__, "cpu_threads": torch.get_num_threads()})
    for year in ([args.year] if args.year else [2021, 2022, 2023]):
        for seed in ([args.seed] if args.seed is not None else [0, 1, 2]):
            case = args.output / f"y{year}-s{seed}"
            if (case / "completed.json").exists():
                continue
            try:
                log(f"START {year} seed={seed}")
                run_case(d, year, seed, case)
                log(f"DONE {year} seed={seed}")
            except BaseException as exc:
                write(args.output / f"y{year}-s{seed}-failure.json", {"error": repr(exc), "at": pd.Timestamp.now(tz="UTC")})
                raise
            total_cpu = sum(json.loads(p.read_text())["cpu_seconds"] for p in args.output.glob("*/completed.json"))
            if total_cpu > 24*3600:
                raise RuntimeError("registered CPU budget exhausted")


if __name__ == "__main__":
    main()
