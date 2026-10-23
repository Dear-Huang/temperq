"""Token-free BaoStock daily data -> a versioned local feed."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qwb.free_data import fetch, table, completed_day, DEFAULT_CODES  # noqa: E402,F401


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", default="data/free_daily")
    parser.add_argument("--codes", default=str(DEFAULT_CODES))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default=completed_day())
    args = parser.parse_args()
    try:
        print(fetch(args.feed, args.codes, args.start, args.end))
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
