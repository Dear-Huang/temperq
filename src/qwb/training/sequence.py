"""Small per-asset GRU baseline with historical-only validation selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import copy
from pathlib import Path
import time

import numpy as np
import torch
from torch import nn

from .. import experiments


@dataclass(frozen=True)
class GRUConfig:
    hidden_size: int = 32
    epochs: int = 20
    batch_size: int = 256
    learning_rate: float = .001
    patience: int = 5
    seed: int = 0
    freeze_encoder: bool = False
    max_seconds: float = 300.0

    def __post_init__(self):
        from ..portfolio.contracts import number

        for name in ("hidden_size", "epochs", "batch_size", "patience"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.seed) is not int or self.seed < 0 or type(self.freeze_encoder) is not bool:
            raise ValueError("invalid seed or encoder freeze setting")
        number(self.learning_rate, "learning rate", lower=1e-8, upper=1)
        number(self.max_seconds, "training time budget", lower=.001)


class SequenceNet(nn.Module):
    def __init__(self, channels, hidden_size):
        super().__init__()
        self.encoder = nn.GRU(channels * 2, hidden_size, batch_first=True)
        self.head = nn.Linear(hidden_size, 1)

    def forward(self, values, valid):
        observed = torch.where(valid, values, torch.zeros_like(values))
        encoded, _ = self.encoder(torch.cat((observed, valid.to(values.dtype)), dim=-1))
        return self.head(encoded[:, -1]).squeeze(-1)


class GRUPredictor:
    capabilities = {"adapter": "sequence_gru_v1", "inputs": ["causal_numeric_window"],
                    "outputs": ["prediction"], "objectives": ["prediction_mse"],
                    "cross_asset_attention": False, "dynamic_asset_count": True,
                    "device": "cpu", "resume": "whole_fold_only"}

    def __init__(self, config=GRUConfig(), *, channels=6):
        if type(channels) is not int or channels < 1:
            raise ValueError("channel count must be a positive integer")
        self.config, self.channels = config, channels
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(config.seed)
            self.net = SequenceNet(channels, config.hidden_size)
        self.history = []
        self.fit_record = None
        self.label_mean, self.label_scale = 0.0, 1.0

    def _validate(self, values, valid):
        values, valid = np.asarray(values), np.asarray(valid)
        if (values.ndim != 3 or values.shape != valid.shape or values.shape[-1] != self.channels
                or values.shape[0] == 0 or values.shape[1] == 0 or valid.dtype != bool
                or not np.isfinite(values).all()):
            raise ValueError("model expects finite sample x lookback x channel values and boolean masks")
        if not valid[:, :, 3 if self.channels >= 4 else 0].any(axis=1).all():
            raise ValueError("each model sample needs an observed price history")
        return torch.tensor(values, dtype=torch.float32), torch.tensor(valid, dtype=torch.bool)

    def fit(self, train_values, train_valid, train_labels, validation_values,
            validation_valid, validation_labels, *, context, log=lambda *_: None):
        """Context binds exact purged fold masks; validation is never used for gradients."""
        self.fit_record = None
        required = {"fit_cutoff", "selection_cutoff", "max_train_label_available", "max_validation_label_available"}
        if not required <= set(context):
            raise ValueError("training context must include label maturity evidence")
        from ..portfolio.contracts import timestamp

        if (timestamp(context["max_train_label_available"]) > timestamp(context["fit_cutoff"])
                or timestamp(context["max_validation_label_available"]) > timestamp(context["selection_cutoff"])):
            raise ValueError("training or validation labels were unavailable at their cutoff")
        x, mask = self._validate(train_values, train_valid)
        vx, vm = self._validate(validation_values, validation_valid)
        y = torch.tensor(np.asarray(train_labels), dtype=torch.float32)
        vy = torch.tensor(np.asarray(validation_labels), dtype=torch.float32)
        if y.shape != (len(x),) or vy.shape != (len(vx),) or not torch.isfinite(y).all() or not torch.isfinite(vy).all():
            raise ValueError("prediction labels must be aligned and finite")
        self.label_mean = float(y.mean())
        self.label_scale = max(float(y.std(unbiased=False)), 1e-8)
        normalized_y = (y - self.label_mean) / self.label_scale
        if self.config.freeze_encoder:
            for parameter in self.net.encoder.parameters():
                parameter.requires_grad_(False)
        optimizer = torch.optim.Adam((p for p in self.net.parameters() if p.requires_grad), lr=self.config.learning_rate)
        rng = torch.Generator().manual_seed(self.config.seed)
        best, best_epoch, stale, best_weights = float("inf"), 0, 0, None
        started = time.monotonic()
        self.history = []
        for epoch in range(self.config.epochs):
            self.net.train()
            order = torch.randperm(len(x), generator=rng)
            total = 0.0
            for start in range(0, len(x), self.config.batch_size):
                if time.monotonic() - started > self.config.max_seconds:
                    raise ValueError("predeclared GRU training time budget exceeded")
                idx = order[start:start + self.config.batch_size]
                optimizer.zero_grad(set_to_none=True)
                loss = nn.functional.mse_loss(self.net(x[idx], mask[idx]), normalized_y[idx])
                if not torch.isfinite(loss):
                    raise ValueError("nonfinite prediction loss")
                loss.backward()
                if any(p.grad is not None and not torch.isfinite(p.grad).all() for p in self.net.parameters()):
                    raise ValueError("nonfinite prediction gradient")
                nn.utils.clip_grad_norm_(self.net.parameters(), 1.0)
                optimizer.step()
                total += float(loss.detach()) * len(idx)
            self.net.eval()
            with torch.no_grad():
                errors = []
                for start in range(0, len(vx), self.config.batch_size):
                    pred = (self.net(vx[start:start + self.config.batch_size], vm[start:start + self.config.batch_size])
                            * self.label_scale + self.label_mean)
                    errors.append(float(torch.sum((pred - vy[start:start + self.config.batch_size]) ** 2)))
                validation = sum(errors) / len(vx)
            if not np.isfinite(validation):
                raise ValueError("nonfinite validation loss")
            train_mse = total / len(x) * self.label_scale ** 2
            self.history.append({"epoch": epoch + 1, "train_mse": train_mse, "validation_mse": validation,
                                 "normalized_train_mse": total / len(x)})
            log(f"GRU epoch {epoch + 1}: train_mse={train_mse:.8g} validation_mse={validation:.8g}")
            if validation < best:
                best, best_epoch, stale = validation, epoch + 1, 0
                best_weights = copy.deepcopy(self.net.state_dict())
            else:
                stale += 1
            if stale >= self.config.patience:
                break
        if time.monotonic() - started > self.config.max_seconds:
            raise ValueError("predeclared GRU training time budget exceeded")
        self.net.load_state_dict(best_weights)
        self.net.eval()
        self.fit_record = {"config": asdict(self.config), "capabilities": self.capabilities,
                           "best_epoch": best_epoch, "validation_mse": best,
                           "n_train": len(x), "n_validation": len(vx), "context": context,
                           "elapsed_seconds": time.monotonic() - started,
                           "history": self.history, "refit_on_validation": False}
        self.fit_record["label_transform"] = {"recipe": "train_only_center_scale_v1", "mean": self.label_mean,
                                               "scale": self.label_scale, "scale_floor": 1e-8,
                                               "reported_prediction_and_mse_units": "raw_decimal_return"}
        return self.fit_record

    def predict(self, values, valid):
        if self.fit_record is None:
            raise ValueError("model has not been fitted")
        x, mask = self._validate(values, valid)
        self.net.eval()
        with torch.no_grad():
            result = torch.cat([self.net(x[start:start + self.config.batch_size], mask[start:start + self.config.batch_size])
                                for start in range(0, len(x), self.config.batch_size)]).numpy()
            result = result * self.label_scale + self.label_mean
        if not np.isfinite(result).all():
            raise ValueError("nonfinite model predictions")
        return result

    def save(self, directory):
        if self.fit_record is None:
            raise ValueError("model has not been fitted")
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=False)
        torch.save(self.net.state_dict(), root / "weights.pt")
        experiments.write(root / "model.json", {"schema_version": 2, "channels": self.channels,
                          "config": asdict(self.config), "fit": self.fit_record,
                          "weights_hash": experiments.digest(root / "weights.pt")})

    @classmethod
    def load(cls, directory):
        root = Path(directory)
        meta = experiments.read(root / "model.json")
        if meta["schema_version"] not in {1, 2} or experiments.digest(root / "weights.pt") != meta["weights_hash"]:
            raise ValueError("unsupported or modified GRU checkpoint")
        model = cls(GRUConfig(**meta["config"]), channels=meta["channels"])
        model.net.load_state_dict(torch.load(root / "weights.pt", map_location="cpu", weights_only=True))
        model.net.eval()
        model.fit_record = meta["fit"]
        model.history = meta["fit"]["history"]
        if meta["schema_version"] == 2:
            from ..portfolio.contracts import number

            transform = meta["fit"]["label_transform"]
            model.label_mean = number(transform["mean"], "label mean", lower=-float("inf"))
            model.label_scale = number(transform["scale"], "label scale", lower=1e-8)
        return model
