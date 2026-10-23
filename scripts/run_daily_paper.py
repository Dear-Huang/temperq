"""Update full-market data, then tick one frozen account only after a freshness check."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qwb import daily_operation, pipeline

if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--account", default="lgbm_forward_20261023")
    parser.add_argument("--feed")
    parser.add_argument("--activate", action="store_true", help="bind an unstarted account; preserve its prior configuration")
    args = parser.parse_args()
    local = pipeline.find_local_config(ROOT)
    feed = Path(args.feed) if args.feed else Path(local["data_dir"]).parent / "qwb_full_market_live"
    if args.activate:
        daily_operation.activate_unstarted(ROOT, args.account, feed)
    result = daily_operation.run(ROOT, args.account, feed)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    raise SystemExit(int(result["state"] == "BLOCKED"))
