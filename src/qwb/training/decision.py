"""Causal GRU-to-allocation policy shared by continuous and exact accounts."""

from __future__ import annotations

from dataclasses import asdict
import copy
from pathlib import Path

import torch

from .. import experiments
from ..portfolio.continuous import ContinuousState, tensor
from ..portfolio.contracts import TargetWeights, timestamp, validate_target
from ..portfolio.episode import ContinuousIntent
from ..portfolio.lifecycle import LifecycleAccountState, LifecycleTargetWeights, Terminal
from ..portfolio.optimization import AllocationConfig, AllocationRejection, ConvexAllocator
from ..portfolio.production import tensor_produce, tensor_produce_dust
from ..portfolio.recipes import RiskRecipe, CostRecipe, HistoryUnavailable, estimate_tensors
from .sequence import GRUPredictor, SequenceNet


class DecisionGRU:
    """Float64 copy of a prediction checkpoint; utility weights have a new schema."""

    def __init__(self, net, *, label_mean, label_scale, source):
        self.net = net.double()
        self.label_mean, self.label_scale = label_mean, label_scale
        self.source = copy.deepcopy(source)
        self.fit_record = None
        self.training_state = None

    @classmethod
    def from_prediction(cls, directory):
        directory = Path(directory)
        model = GRUPredictor.load(directory)
        return cls(copy.deepcopy(model.net), label_mean=model.label_mean, label_scale=model.label_scale,
                   source={"path": str(directory.resolve()), "weights_hash": experiments.digest(directory / "weights.pt"),
                           "config": asdict(model.config), "fit": model.fit_record})

    @classmethod
    def from_run(cls, directory, *, fold, seed, bundle):
        """Select a checkpoint through a verified immutable prediction archive."""
        if type(fold) is not int or fold < 0 or type(seed) is not int or seed < 0:
            raise ValueError("source fold and seed must be nonnegative integers")
        root = Path(directory).resolve()
        record = experiments.verify(root)
        if record["model"] != "sequence_gru_v1" or record["task"] != "cn_predict_intraday_v1":
            raise ValueError("utility initialization needs an intraday GRU prediction archive")
        if record["data_content_hash"] != bundle.identity["content_hash"]:
            raise ValueError("utility initialization and observations have different data content")
        manifest = experiments.read(root / "folds.json")
        matches = [row for row in manifest["folds"] if row["fold"] == fold]
        if len(matches) != 1:
            raise ValueError("source fold is missing or ambiguous")
        selected = matches[0]
        if selected["observation_recipe"] != bundle.windows.manifest["recipe_hash"]:
            raise ValueError("utility initialization observation recipe differs")
        seeds = [row for row in selected["seeds"] if row["seed"] == seed]
        if len(seeds) != 1:
            raise ValueError("source seed is missing or ambiguous")
        relative = seeds[0]["checkpoint"]
        checkpoint = (root / relative).resolve()
        if not checkpoint.is_relative_to(root) or f"{relative}/model.json" not in record["artifacts"]:
            raise ValueError("source checkpoint is not a verified archived artifact")
        model = cls.from_prediction(checkpoint)
        if model.source["fit"] != seeds[0]["fit"]:
            raise ValueError("source checkpoint fit and fold lineage differ")
        model.source.update(run_id=record["run_id"], artifact_hash=record["artifact_hash"],
            checkpoint=relative, fold=fold, seed=seed, data_content_hash=record["data_content_hash"],
            observation_recipe=selected["observation_recipe"], task=record["task"],
            dtype_conversion="source_float32_weights_to_cpu_float64")
        return model

    def forecast(self, values, valid):
        return self.net(values, valid) * self.label_scale + self.label_mean

    def save(self, directory):
        if self.fit_record is None or self.training_state is None:
            raise ValueError("decision model has not been trained")
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=False)
        torch.save(self.net.state_dict(), root / "weights.pt")
        torch.save(self.training_state, root / "training_state.pt")
        experiments.write(root / "model.json", {"schema_version": "decision_gru_v1", "dtype": "float64",
            "channels": self.net.encoder.input_size // 2, "hidden_size": self.net.encoder.hidden_size,
            "label_mean": self.label_mean, "label_scale": self.label_scale, "source": self.source,
            "fit": self.fit_record, "resume": "whole_fold_only_no_mid_episode_resume",
            "weights_hash": experiments.digest(root / "weights.pt"),
            "training_state_hash": experiments.digest(root / "training_state.pt")})

    @classmethod
    def load(cls, directory):
        from ..portfolio.contracts import number

        root = Path(directory)
        meta = experiments.read(root / "model.json")
        if (meta["schema_version"] != "decision_gru_v1" or meta["dtype"] != "float64"
                or experiments.digest(root / "weights.pt") != meta["weights_hash"]
                or experiments.digest(root / "training_state.pt") != meta["training_state_hash"]):
            raise ValueError("unsupported or changed decision checkpoint")
        number(meta["label_mean"], "label mean", lower=-float("inf"))
        number(meta["label_scale"], "label scale", lower=1e-8)
        if any(type(meta[key]) is not int or meta[key] < 1 for key in ("channels", "hidden_size")):
            raise ValueError("invalid decision checkpoint dimensions")
        with torch.random.fork_rng(devices=[]):
            net = SequenceNet(meta["channels"], meta["hidden_size"]).double()
        weights = torch.load(root / "weights.pt", map_location="cpu", weights_only=True)
        if any(not isinstance(p, torch.Tensor) or p.dtype != torch.float64 for p in weights.values()):
            raise ValueError("decision checkpoint requires float64 weights")
        net.load_state_dict(weights)
        if any(not bool(torch.isfinite(p).all()) for p in net.parameters()):
            raise ValueError("nonfinite decision checkpoint weights")
        model = cls(net, label_mean=meta["label_mean"], label_scale=meta["label_scale"], source=meta["source"])
        model.fit_record = meta["fit"]
        model.training_state = torch.load(root / "training_state.pt", map_location="cpu", weights_only=True)
        if model.fit_record["config"]["freeze_encoder"]:
            for parameter in model.net.encoder.parameters():
                parameter.requires_grad_(False)
        model.net.eval()
        return model


class DecisionPolicy:
    def __init__(self, model, bundle, *, risk, cost, optimizer, fees, min_history,
                 production_buffer=1e-12, production_policy="torch_box_budget_turnover_v1", dust_threshold=None,
                 fee_model="declared_execution", model_version="local-decision-gru"):
        self.model, self.bundle = model, bundle
        self.risk, self.cost = RiskRecipe(**risk), CostRecipe(**cost)
        self.optimizer, self.fees = AllocationConfig(**optimizer), fees
        if type(min_history) is not int or not 1 <= min_history <= bundle.windows.lookback:
            raise ValueError("invalid decision policy minimum history")
        if fee_model not in {"none", "declared_execution", "causal_linear"}:
            raise ValueError("invalid decision policy fee model")
        from ..portfolio.contracts import number

        if production_policy not in {"torch_box_budget_turnover_v1", "torch_box_budget_turnover_dust_v1"}:
            raise ValueError("unsupported decision production policy")
        number(production_buffer, "production buffer", lower=1e-15, upper=min(self.optimizer.tolerance, 1e-9))
        if production_policy == "torch_box_budget_turnover_dust_v1":
            number(dust_threshold, "target dust threshold", lower=1e-15, upper=min(self.optimizer.tolerance, 1e-8))
        elif dust_threshold is not None:
            raise ValueError("dust threshold requires the explicit dust production policy")
        self.production_policy, self.dust_threshold = production_policy, dust_threshold
        self.min_history, self.production_buffer, self.fee_model = min_history, production_buffer, fee_model
        self.model_version, self.layers, self.audit = model_version, {}, []

    def action(self, context):
        detail = {"schema_version": "decision_policy_error_v1", "decision_id": context.decision_id,
                  "decision_time": str(context.state.observed_at), "stage": "preflight"}
        try:
            return self._action(context, detail)
        except Exception as exc:
            if isinstance(exc, (AllocationRejection, HistoryUnavailable)):
                detail["failure_kind"] = {"allocation": "allocation_rejected", "production": "production_rejected",
                                          "risk_cost_recipe": "recipe_history"}.get(detail["stage"])
            detail.update(exception_type=type(exc).__name__, reason=str(exc))
            exc.qwb_decision_failure = detail
            raise

    def _action(self, context, detail):
        continuous = isinstance(context.state, ContinuousState)
        state = context.state
        assets = state.assets if continuous else context.assets
        detail.update(assets=list(assets), data_snapshot_id=(self.bundle.manifest["data_snapshot_id"]
                                                          if continuous else state.data_snapshot_id))
        if context.max_weight != self.optimizer.max_weight or context.min_cash != self.optimizer.min_cash:
            raise ValueError("decision policy constraints differ from the account protocol")
        eligible = set(context.eligible_assets)
        _, visible = self.bundle.universe_at(state.observed_at)
        if visible != eligible:
            raise ValueError("decision policy universe differs from causal observations")
        detail["stage"] = "observations"
        observations = self.bundle.windows.at(state.observed_at, assets, eligible=eligible)
        detail["history_lengths"] = observations.lengths[0].tolist()
        indices = [i for i, code in enumerate(assets) if code in eligible]
        if any(observations.lengths[0, i] < self.min_history or not observations.valid[0, i, -1, :4].all() for i in indices):
            detail["failure_kind"] = "observation_history"
            raise ValueError("eligible decision asset lacks current or sufficient history")
        detail["stage"] = "forecast"
        if indices:
            values = tensor(observations.values[0, indices].copy())
            valid = torch.tensor(observations.valid[0, indices].copy(), dtype=torch.bool)
            predicted = self.model.forecast(values, valid)
            if (not isinstance(predicted, torch.Tensor) or predicted.shape != (len(indices),)
                    or predicted.dtype != torch.float64 or predicted.device.type != "cpu"):
                raise ValueError("decision forecast requires aligned float64 CPU predictions")
            if not bool(torch.isfinite(predicted).all()):
                detail.update(failure_kind="nonfinite_forecast", prediction_repr=repr(predicted.detach().tolist()))
                raise ValueError("nonfinite decision forecast")
            by_index = {i: predicted[j] for j, i in enumerate(indices)}
            mu = torch.stack([by_index.get(i, predicted.new_zeros(())) for i in range(len(assets))])
        else:
            mu = tensor([0.] * len(assets))
        detail["prediction_repr"] = repr(mu.detach().tolist())
        detail["stage"] = "known_constraints"
        equity = state.equity if continuous else tensor(state.equity)
        previous = state.weights if continuous else tensor(state.weights(assets))
        receivable = state.receivable if continuous else tensor(state.receivable_equity)
        reserve = receivable / equity
        lower = []
        terminal = {event.asset_id for event in context.known_actions if isinstance(event, Terminal)
                    and timestamp(event.effective_at) < timestamp(context.execution_not_before)}
        upper = []
        exact_positions = {} if continuous else {position.asset_id: position for position in state.positions}
        for i, code in enumerate(assets):
            if continuous:
                locked = sum((lot.quantity[i] for lot in state.lots
                              if timestamp(lot.sellable_after) > timestamp(context.execution_not_before)), equity.new_zeros(()))
                bound = locked * state.marks[i] / equity
            else:
                p = exact_positions.get(code)
                locked = sum(lot.quantity for lot in p.unsettled
                             if timestamp(lot.sellable_after) > timestamp(context.execution_not_before)) if p else 0
                bound = tensor(locked * p.mark / state.equity if p else 0.)
            hi = previous.new_tensor(context.max_weight)
            if code not in eligible:
                hi = torch.minimum(hi, previous[i])
            if code in terminal:
                bound, hi = bound * 0, hi * 0
            lower.append(bound)
            upper.append(hi)
        lower, upper = torch.stack(lower), torch.stack(upper)
        detail["constraints"] = {"previous_weights": previous.detach().tolist(),
            "lower": lower.detach().tolist(), "upper": upper.detach().tolist(),
            "budget": float((1 - reserve - context.min_cash).detach()),
            "receivable_weight": float(reserve.detach()), "max_turnover": self.optimizer.max_turnover,
            "max_weight": context.max_weight, "min_cash": context.min_cash,
            "production_policy": self.production_policy, "tolerance": self.optimizer.tolerance}
        detail["stage"] = "risk_cost_recipe"
        root, buy, sell, recipe = estimate_tensors(self.bundle, state.observed_at, assets, equity=equity,
            fees=self.fees, risk=self.risk, cost=self.cost)
        detail["recipe_id"] = recipe["estimate_id"]
        detail["stage"] = "allocation"
        if assets not in self.layers:
            self.layers[assets] = ConvexAllocator(len(assets), self.optimizer)
        raw, solve = self.layers[assets](mu, root, previous, buy, sell, lower=lower, upper=upper, receivable_weight=reserve)
        detail.update(raw_weights=raw.detach().tolist(), allocation=solve, stage="production")
        produce = tensor_produce_dust if self.dust_threshold is not None else tensor_produce
        extra = {"dust_threshold": self.dust_threshold} if self.dust_threshold is not None else {}
        produced, production = produce(raw, previous, lower, upper,
            budget=1 - reserve - context.min_cash, max_turnover=self.optimizer.max_turnover,
            tolerance=self.optimizer.tolerance, buffer=self.production_buffer, **extra)
        if self.dust_threshold is not None:
            production["suppressed_decision_notional_cny"] = float(equity.detach()) * production["suppressed_weight"]
        self.audit.append({"decision_id": context.decision_id, "assets": assets, "decision_time": state.observed_at,
                           "eligible_assets": sorted(eligible),
                           "production_inputs": {"previous_weights": previous.detach().tolist(),
                               "lower": lower.detach().tolist(), "upper": upper.detach().tolist(),
                               "budget": float((1 - reserve - context.min_cash).detach()),
                               "receivable_weight": float(reserve.detach()), "min_cash": context.min_cash,
                               "max_weight": context.max_weight,
                               "max_turnover": self.optimizer.max_turnover, "tolerance": self.optimizer.tolerance},
                           "prediction": mu.detach().tolist(), "recipe": recipe, "allocation": solve, "production": production})
        return produced, (buy, sell)

    def __call__(self, context):
        weights, rates = self.action(context)
        if isinstance(context.state, ContinuousState):
            return ContinuousIntent(weights, rates if self.fee_model == "causal_linear" else None)
        state = context.state
        reserve = state.receivable_equity / state.equity
        cls = LifecycleTargetWeights if isinstance(state, LifecycleAccountState) else TargetWeights
        extra = {"receivable_weight": reserve} if cls is LifecycleTargetWeights else {}
        try:
            target = cls(context.decision_id, state.observed_at, context.execution_not_before, "cn_alloc_daily_v1",
                self.model_version, state.data_snapshot_id, state.state_id, context.universe_id, context.protocol_id,
                tuple(zip(context.assets, weights.detach().tolist())), float(1 - reserve - weights.detach().sum()), **extra)
            validate_target(target, state, assets=context.assets, universe_id=context.universe_id, protocol_id=context.protocol_id,
                            execution_at=context.execution_not_before, max_weight=context.max_weight, min_cash=context.min_cash)
        except Exception as exc:
            exc.qwb_decision_failure = {"schema_version": "decision_policy_error_v1",
                "decision_id": context.decision_id, "decision_time": str(state.observed_at),
                "stage": "target_validation", "action_audit": self.audit[-1],
                "exception_type": type(exc).__name__, "reason": str(exc)}
            raise
        return target
