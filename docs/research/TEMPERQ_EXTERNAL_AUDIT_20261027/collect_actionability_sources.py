"""Collect public practice, code and issue evidence; performs read-only requests."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import re
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent / "evidence" / "actionability"
JOBS = [
    ("blog_buffering", "https://qoppac.blogspot.com/feeds/posts/default?alt=json&q=buffering&max-results=8"),
    ("aqr_trading_costs", "https://www.aqr.com/Insights/Research/Working-Paper/Trading-Costs"),
    ("aqr_anomaly_costs", "https://www.aqr.com/Insights/Research/Working-Paper/Trading-Costs-of-Asset-Pricing-Anomalies"),
    ("cvx_examples", "https://www.cvxportfolio.com/en/stable/examples.html"),
    ("cvx_policies", "https://www.cvxportfolio.com/en/stable/policies.html"),
    ("lean_min_orders", "https://www.quantconnect.com/docs/v2/writing-algorithms/trading-and-orders/position-sizing"),
    ("skfolio_costs", "https://skfolio.org/auto_examples/mean_risk/plot_6_transaction_costs.html"),
]
QUERIES = {
    "cvx_cost_issues": "repo:cvxgrp/cvxportfolio transaction costs",
    "cvx_infeasible_issues": "repo:cvxgrp/cvxportfolio infeasible",
    "carver_buffer_issues": "repo:robcarver17/pysystemtrade buffering",
    "lean_small_orders": "repo:QuantConnect/Lean minimum order fee",
}


def redact_public_mail_links(value):
    """Do not retain or print notification-auth links in public mail footers."""
    if isinstance(value, str):
        return re.sub(
            r'https?://[^\s"<>]*notifications/unsubscribe-auth/[^\s"<>]+',
            '[REDACTED_PUBLIC_MAIL_FOOTER]',
            value,
        )
    if isinstance(value, list):
        return [redact_public_mail_links(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_public_mail_links(item) for key, item in value.items()}
    return value

def fetch(key, url, params=None):
    item = {"key": key, "url": url, "params": params, "retrieved_utc": datetime.now(timezone.utc).isoformat()}
    try:
        r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "TemperQ-public-research/1.0"})
        item.update(status=r.status_code, final_url=r.url, content_type=r.headers.get("Content-Type", ""))
        if "json" in item["content_type"]:
            data = redact_public_mail_links(r.json()); (ROOT / (key + ".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
            item["file"] = key + ".json"
            if isinstance(data, dict) and "items" in data:
                item["results"] = [{k: x.get(k) for k in ["title", "html_url", "body", "comments", "state", "number"]} for x in data["items"]]
        else:
            soup = BeautifulSoup(r.content, "html.parser")
            for tag in soup(["script", "style", "nav", "footer"]): tag.decompose()
            text = redact_public_mail_links(soup.get_text("\n", strip=True))
            (ROOT / (key + ".txt")).write_text(text, encoding="utf8");item["file"] = key + ".txt";item["chars"] = len(text)
        if not r.ok: item["error"] = "http_error"
    except Exception as exc: item["error"] = str(exc)
    return item

def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    jobs = [(k, u, None) for k, u in JOBS]
    jobs += [(k, "https://api.github.com/search/issues", {"q": q, "per_page": 5}) for k, q in QUERIES.items()]
    rows = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch, *job) for job in jobs]
        for future in as_completed(futures):
            row = future.result(); rows.append(row)
            print(json.dumps(row, ensure_ascii=False)[:10000], flush=True)
    (ROOT / "source_manifest.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf8")

if __name__ == "__main__": main()
