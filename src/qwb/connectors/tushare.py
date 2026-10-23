"""Tushare-compatible HTTP client (SPEC §4.6).

Plain HTTP instead of the tushare SDK: the SDK's set_token() writes the
token into a file under the home directory, and compatible vendors only
need the endpoint changed.

Three rules from past incidents are enforced here:
- a non-zero `code` or a malformed body raises ApiError; an empty item
  list is a valid "no rows" answer and is returned as an empty frame;
- requests are spaced by `min_interval` seconds;
- offset paging is checked for overlap between pages, because some
  vendors return unstable orderings on bulk queries (seen on namechange).
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import pandas as pd

DEFAULT_URL = "http://api.tushare.pro"
TOKEN_ENV = "TEMPERQ_TUSHARE_TOKEN"
RETRY_CODES = {429, 40203}      # vendor throttling; 40203 = per-minute quota on tushare


class ApiError(RuntimeError):
    pass


def load_token(token_file: str | None = None) -> str:
    """Token from the environment (including legacy QWB), then a file. Never logged."""
    tok = os.environ.get(TOKEN_ENV, os.environ.get("QWB_TUSHARE_TOKEN", "")).strip()
    if tok:
        return tok
    if token_file and Path(token_file).is_file():
        tok = Path(token_file).read_text(encoding="utf-8").strip()
        if tok:
            return tok
    raise ApiError(f"no tushare token: set ${TOKEN_ENV} or `tushare.token_file` in config")


def _http(url: str, payload: dict, timeout: float) -> dict:
    # gzip: some vendors throttle (code 429) clients that do not accept it
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if r.headers.get("Content-Encoding", "").lower() == "gzip" or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return json.loads(body.decode("utf-8"))


class TushareClient:
    def __init__(self, token: str, url: str = DEFAULT_URL, min_interval: float = 0.2,
                 timeout: float = 60, retries: int = 3,
                 transport: Callable[[str, dict, float], dict] | None = None):
        self._token = token
        self.url = url
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._transport = transport or _http
        self._last = 0.0

    def __repr__(self) -> str:  # keep the token out of logs and tracebacks
        return f"TushareClient(url={self.url!r})"

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()

    def call(self, api: str, fields: str = "", **params) -> pd.DataFrame:
        payload = {"api_name": api, "token": self._token, "params": params, "fields": fields}
        out = None
        for attempt in range(self.retries + 1):
            self._wait()
            try:
                out = self._transport(self.url, payload, self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                if attempt == self.retries:
                    raise ApiError(f"{api} {params}: transport failed after retries: "
                                   f"{type(e).__name__}") from None
                time.sleep(min(2 ** attempt, 10))
                continue
            # rate limiting is transient: back off and retry, never return it as data
            if isinstance(out, dict) and out.get("code") in RETRY_CODES and attempt < self.retries:
                time.sleep(min(5 * 2 ** attempt, 60))
                continue
            break
        if not isinstance(out, dict) or out.get("code") != 0:
            code = out.get("code") if isinstance(out, dict) else None
            msg = out.get("msg") if isinstance(out, dict) else "non-dict body"
            raise ApiError(f"{api} {params}: code={code} msg={msg}")
        data = out.get("data")
        if not isinstance(data, dict) or "fields" not in data:
            raise ApiError(f"{api} {params}: malformed body (no data.fields)")
        return pd.DataFrame(data.get("items") or [], columns=data["fields"])

    def paged(self, api: str, page_size: int, key_cols: list[str], max_pages: int = 200,
              **params) -> pd.DataFrame:
        frames, seen = [], set()
        for page in range(max_pages):
            df = self.call(api, limit=page_size, offset=page * page_size, **params)
            if df.empty:  # some vendors omit fields on empty answers
                frames.append(df)
                return pd.concat(frames, ignore_index=True) if page else df
            missing = [c for c in key_cols if c not in df.columns]
            if missing:
                raise ApiError(f"{api} {params}: response lacks key columns {missing}")
            keys = set(map(tuple, df[key_cols].astype(str).itertuples(index=False)))
            if keys & seen:
                raise ApiError(f"{api} {params}: page {page} overlaps earlier pages; "
                               "vendor ordering is unstable, narrow the query")
            seen |= keys
            frames.append(df)
            if len(df) < page_size:
                return pd.concat(frames, ignore_index=True)
        raise ApiError(f"{api} {params}: more than {max_pages} pages")
