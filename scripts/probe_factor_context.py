"""Synthetic CPU feasibility probe, not a market experiment or trading policy."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import torch
from torch import nn


class FactorContext(nn.Module):
    def __init__(self, features: int, width: int = 64, tokens: int = 16):
        super().__init__()
        self.encode = nn.Sequential(
            nn.Linear(features, width), nn.GELU(), nn.Linear(width, width)
        )
        self.queries = nn.Parameter(torch.randn(tokens, width) / width**0.5)
        self.pool = nn.MultiheadAttention(width, 4, batch_first=True)
        self.context = nn.MultiheadAttention(width, 4, batch_first=True)
        self.head = nn.Sequential(
            nn.Linear(2 * width, width), nn.GELU(), nn.Linear(width, 1)
        )

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        encoded = self.encode(features)
        queries = self.queries.unsqueeze(0).expand(features.shape[0], -1, -1)
        pooled, _ = self.pool(queries, encoded, encoded, need_weights=False)
        context, _ = self.context(encoded, pooled, pooled, need_weights=False)
        scores = self.head(torch.cat((encoded, context), dim=-1)).squeeze(-1)
        return scores.softmax(dim=-1)


def probe(assets: int, features: int, steps: int) -> dict:
    torch.manual_seed(20261026)
    model = FactorContext(features)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
    x = torch.randn(2, assets, features)
    returns = 0.01 * torch.randn(2, assets)
    previous = torch.full((2, assets), 1.0 / assets)
    durations = []
    norms = []
    for step in range(steps + 2):
        started = time.perf_counter()
        optimizer.zero_grad(set_to_none=True)
        weights = model(x)
        gross = (weights * returns).sum(-1)
        # Smooth cost only tests the differentiable path; it is not the ledger.
        turnover = ((weights - previous).square() + 1e-14).sqrt().sum(-1)
        loss = -(gross - 0.0003 * turnover).mean()
        loss.backward()
        squared_norm = sum(
            p.grad.detach().square().sum().item()
            for p in model.parameters()
            if p.grad is not None
        )
        optimizer.step()
        if step >= 2:
            durations.append(time.perf_counter() - started)
            norms.append(squared_norm**0.5)
    return {
        "assets": assets,
        "features": features,
        "synthetic_dates_per_step": 2,
        "parameters": sum(p.numel() for p in model.parameters()),
        "measured_steps": steps,
        "mean_forward_backward_update_seconds": sum(durations) / len(durations),
        "gradient_l2_min": min(norms),
        "gradient_l2_max": max(norms),
        "finite_nonzero_gradient": all(0 < n < float("inf") for n in norms),
        "input_mib": x.numel() * x.element_size() / 2**20,
        "single_date_materialized_full_attention_mib_4_heads": assets**2 * 4 * 4 / 2**20,
        "single_date_two_cross_attention_matrices_mib_4_heads": 2 * assets * 16 * 4 * 4 / 2**20,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--steps", type=int, default=5)
    args = parser.parse_args()
    if args.steps < 1:
        parser.error("steps must be positive")
    torch.set_num_threads(2)
    report = {
        "scope": "SYNTHETIC_COMPUTE_PROBE_ONLY",
        "torch": torch.__version__,
        "device": "cpu",
        "threads": torch.get_num_threads(),
        "cuda_available_in_environment": torch.cuda.is_available(),
        "seed": 20261026,
        "scenarios": [probe(500, 32, args.steps), probe(5000, 158, args.steps)],
        "limitations": [
            "No market data, predictive ability, or economic improvement measured.",
            "Softmax allocation omits concentration, lots, T+1, and market restrictions.",
            "Smooth cost is not the exact execution ledger or its qualified surrogate.",
            "Fixed feature schema; arbitrary unseen user factors are not supported.",
            "Bottleneck attention is a proposed approximation, not a reproduction of AIPM.",
            "Nonzero gradients here do not validate or fix the existing B2 solver.",
            "Matrix sizes are materialized float32 arithmetic, not measured peak memory.",
            "Step timing is a warmed synthetic measurement, not full training time.",
        ],
    }
    encoded = json.dumps(report, indent=2, ensure_ascii=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
