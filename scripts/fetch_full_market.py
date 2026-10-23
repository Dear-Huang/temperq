"""Fetch complete full-market daily snapshots using private local API settings."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qwb.full_market import update

if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--feed", required=True)
    parser.add_argument("--end")
    args = parser.parse_args()
    try:
        print(update(ROOT, args.feed, args.end), flush=True)
    except Exception as exc:
        print(f"Full-market fetch stopped ({type(exc).__name__}); no partial snapshot published", file=sys.stderr)
        raise SystemExit(1)
