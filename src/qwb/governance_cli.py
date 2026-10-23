"""CLI adapter for the same governance experiment used by the local UI."""
from pathlib import Path
import json
import sys
import uuid

from . import governance


def add_parser(sub):
    parser = sub.add_parser("governance", help="historical rules/EWMA/ML risk comparison (CPU)")
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--cash", type=float, default=100000)
    parser.add_argument("--output", help="new directory, never overwritten")
    parser.add_argument("--n-hold", type=int, default=10)
    parser.add_argument("--hold-until-rank", type=int, default=15)
    parser.add_argument("--max-weight", type=float, default=.1)
    parser.add_argument("--exposure-cap", type=float, default=1.)
    parser.add_argument("--cash-floor", type=float, default=0.)
    parser.add_argument("--target-volatility", type=float, default=.15)
    parser.add_argument("--slippage-bp", type=float, default=0.)


def run(args):
    try:
        cfg = governance.GovernanceConfig(**{key: getattr(args, key) for key in
            ("n_hold", "hold_until_rank", "max_weight", "exposure_cap", "cash_floor", "target_volatility", "slippage_bp")})
        output = Path(args.output) if args.output else Path.cwd()/"runs/governance"/uuid.uuid4().hex
        result = governance.run(args.predictions, args.data_dir, output, cfg,
            start=args.start, end=args.end, initial_cash=args.cash, log=lambda msg: print(msg, flush=True))
        print(json.dumps({"output": str(output), "metrics": result["metrics"]}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(f"governance: {exc}", file=sys.stderr)
        return 1
