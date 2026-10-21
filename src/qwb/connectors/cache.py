"""Local request cache and data snapshot id (SPEC §4.6).

Each (api, params) result is one parquet file plus a JSON sidecar. The
snapshot id of a fetch is the hash over the sidecars it used, so two runs
that read identical cached responses share a snapshot id.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd


def _key(api: str, params: dict) -> str:
    blob = json.dumps({"api": api, "params": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


class Cache:
    def __init__(self, root: str | Path, refresh: bool = False):
        self.root = Path(root)
        self.refresh = refresh
        self.used: list[dict] = []

    def get(self, api: str, params: dict, fetch: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        k = _key(api, params)
        pq, meta_p = self.root / api / f"{k}.parquet", self.root / api / f"{k}.json"
        if pq.exists() and meta_p.exists() and not self.refresh:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            df = pd.read_parquet(pq)
        else:
            df = fetch()
            pq.parent.mkdir(parents=True, exist_ok=True)
            # per-process temp name: concurrent fetchers may hit the same key
            tag = f"{os.getpid()}.{uuid.uuid4().hex[:6]}"
            tmp = pq.with_name(f"{k}.{tag}.tmp")
            df.to_parquet(tmp, index=False)
            try:
                tmp.replace(pq)
            except PermissionError:
                # Windows: another process finished the same key first; keep its file
                tmp.unlink(missing_ok=True)
                if not pq.exists():
                    raise
                df = pd.read_parquet(pq)
            meta = {"api": api, "params": params, "rows": len(df),
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "sha256": hashlib.sha256(pq.read_bytes()).hexdigest()}
            mtmp = meta_p.with_name(f"{k}.{tag}.json.tmp")
            mtmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            try:
                mtmp.replace(meta_p)
            except PermissionError:
                mtmp.unlink(missing_ok=True)
        self.used.append(meta)
        return df

    def snapshot_id(self) -> str:
        parts = sorted(f"{m['api']}|{json.dumps(m['params'], sort_keys=True)}|{m['sha256']}"
                       for m in self.used)
        return "snap-" + hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
