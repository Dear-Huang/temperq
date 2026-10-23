"""Update full-market data, then tick one frozen account only after a freshness check."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qwb import daily_operation, paper


def account_feed(workdir, name, override=None):
    source = override or paper.inspect(workdir, name)["config"]["data"].get("dir")
    if not source:
        raise ValueError("Account has no bound daily data feed; specify --feed")
    path = Path(source)
    return path.resolve() if path.is_absolute() else (Path(workdir) / path).resolve()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--account", default="lgbm_forward_20261023")
    parser.add_argument("--feed")
    parser.add_argument("--activate", action="store_true", help="bind an unstarted account; preserve its prior configuration")
    args = parser.parse_args()
    feed = account_feed(ROOT, args.account, args.feed)
    if args.activate:
        daily_operation.activate_unstarted(ROOT, args.account, feed)
    result = daily_operation.run(ROOT, args.account, feed)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    raise SystemExit(int(result["state"] == "BLOCKED"))
