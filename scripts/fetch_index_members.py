"""Add CSI 300 / 500 / 1000 constituent history to an existing dataset.

Usage:  python -X utf8 scripts/fetch_index_members.py [data_dir]
Writes <data_dir>/index_members.parquet (code, index, date = snapshot date).
snapshot.json is not touched, so bench protocols that hash it stay valid.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import qwb  # noqa: E402,F401
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from qwb.connectors import cn_a  # noqa: E402
from qwb.connectors.cache import Cache  # noqa: E402
from qwb.connectors.tushare import TushareClient, load_token  # noqa: E402


def main() -> int:
    local = yaml.safe_load((ROOT / "config.local.yaml").read_text(encoding="utf-8"))
    data_dir = Path(sys.argv[1] if len(sys.argv) > 1 else local["data_dir"])
    snap = json.loads((data_dir / "snapshot.json").read_text(encoding="utf-8"))
    t = local["tushare"]
    cli = TushareClient(load_token(t["token_file"]), url=t["url"],
                        min_interval=t.get("min_interval", 0.25))
    cache = Cache(local["cache_dir"])
    frames = []
    for name, code in cn_a.INDEXES.items():
        df = cn_a.index_members(cli, cache, code, snap["start"], snap["end"])
        print(f"{name} {code}: {df['date'].nunique()} snapshots, {len(df)} rows", flush=True)
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(data_dir / "index_members.parquet", index=False)
    print("wrote", data_dir / "index_members.parquet", len(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
