"""Snapshot public A-share research and code without installing or running it."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import argparse
import base64
import hashlib
import json
import re

import requests
from bs4 import BeautifulSoup

OUT = Path(__file__).resolve().parent / "evidence" / "ashare_round"
REPOS = ["ricequant/rqalpha", "microsoft/qlib", "vnpy/vnpy", "vnpy/vnpy_alpha", "wondertrader/wondertrader", "AI4Finance-Foundation/FinRL"]
SEARCHES = {
    "qlib_t1": "repo:microsoft/qlib T+1",
    "qlib_limit": "repo:microsoft/qlib limit_threshold",
    "qlib_execution": "repo:microsoft/qlib execution backtest",
    "rqalpha_cost": "repo:ricequant/rqalpha min_commission",
    "rqalpha_matching": "repo:ricequant/rqalpha volume_limit",
    "rqalpha_t1": "repo:ricequant/rqalpha T+1",
    "vnpy_stock": "repo:vnpy/vnpy 股票 回测",
}


def clean(value):
    if isinstance(value, str):
        return re.sub(r'https?://[^\s"<>]*notifications/unsubscribe-auth/[^\s"<>]+', '[REDACTED_PUBLIC_MAIL_FOOTER]', value)
    if isinstance(value, list):
        return [clean(x) for x in value]
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    return value


def fetch(key, url, params=None):
    row = {"key": key, "url": url, "params": params, "retrieved_utc": datetime.now(timezone.utc).isoformat()}
    try:
        response = requests.get(url, params=params, timeout=35, headers={"User-Agent": "TemperQ-public-research/1.0"})
        row.update(status=response.status_code, final_url=response.url, content_type=response.headers.get("Content-Type", ""))
        if not response.ok:
            row["error"] = "http_error"
            return row
        if "json" in row["content_type"]:
            data = clean(response.json())
            name = key + ".json"
            (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
            if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
                source = base64.b64decode(data["content"])
                (OUT / (key + ".source")).write_bytes(source)
                row["source_file"] = key + ".source"
        elif response.content.startswith(b"%PDF"):
            name = key + ".pdf"
            (OUT / name).write_bytes(response.content)
            import fitz
            doc = fitz.open(stream=response.content, filetype="pdf")
            (OUT / (key + ".txt")).write_text("\n".join(p.get_text() for p in doc), encoding="utf8")
            row["pages"] = len(doc)
        elif "raw.githubusercontent.com" in response.url:
            name = key + ".source"
            (OUT / name).write_bytes(response.content)
        else:
            soup = BeautifulSoup(response.content, "html.parser")
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            name = key + ".txt"
            (OUT / name).write_text(clean(soup.get_text("\n", strip=True)), encoding="utf8")
        row["file"] = name
        row["sha256"] = hashlib.sha256((OUT / name).read_bytes()).hexdigest()
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=Path)
    parser.add_argument("--manifest", default="initial_manifest.json")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.jobs:
        jobs = json.loads(args.jobs.read_text(encoding="utf8"))
    else:
        jobs = [{"key": repo.replace("/", "_") + "_repo", "url": "https://api.github.com/repos/" + repo} for repo in REPOS]
        jobs += [{"key": key, "url": "https://api.github.com/search/issues", "params": {"q": query, "per_page": 7}} for key, query in SEARCHES.items()]
    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch, **job) for job in jobs]
        for future in as_completed(futures):
            row = future.result()
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    (OUT / args.manifest).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
