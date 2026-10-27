"""Read-only public-source collection for the 2026-10-27 TemperQ review.

Run explicitly; never imports TemperQ or reads user accounts. Network responses
are evidence snapshots, not instructions. No credentials are used.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "evidence"
REPOS = [
    "cvxgrp/cvxportfolio", "skfolio/skfolio", "bashtage/arch",
    "statsmodels/statsmodels", "stefan-jansen/alphalens-reloaded",
    "stefan-jansen/pyfolio-reloaded", "AndrewYChen/OpenSourceAP",
    "bkelly-lab/ReplicationCrisis", "QuantConnect/Lean", "microsoft/qlib",
    "dcajasn/Riskfolio-Lib", "ranaroussi/quantstats",
]
PAGES = {
    "cvx_constraints": "https://www.cvxportfolio.com/en/stable/constraints.html",
    "arch_multiple_comparison": "https://arch.readthedocs.io/en/latest/multiple-comparison/multiple-comparison_examples.html",
    "sr1107": "https://www.federalreserve.gov/supervisionreg/srletters/sr1107.htm",
    "fca_algo_review": "https://www.fca.org.uk/publications/multi-firm-reviews/algorithmic-trading-compliance-wholesale-markets",
    "opensourceap": "https://www.openassetpricing.com/",
    "jkp_data": "https://jkpfactors.com/",
    "novymarx_research": "https://rnm.simon.rochester.edu/research.html",
    "garleanu_pedersen": "https://www.lhpedersen.com/papers/DynamicTrading.pdf",
    "mind_sorts": "https://doi.org/10.1016/j.jempfin.2024.101517",
    "sharpe_lo": "https://web.mit.edu/alo/www/Papers/The%20Statistics%20of%20Sharpe%20Ratios.pdf",
    "dsr2014": "https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf",
    "cvx_paper": "https://web.stanford.edu/~boyd/papers/pdf/cvx_portfolio.pdf",
    "ssrn_dsr2026": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7198158",
    "new_dsr_jpm": "https://www.pm-research.com/content/iijpormgmt/early/2026/09/26/jpm.2026.070",
}

def fetch(kind, key, url):
    stamp = datetime.now(timezone.utc).isoformat()
    result = {"kind": kind, "key": key, "url": url, "retrieved_utc": stamp}
    try:
        r = requests.get(url, timeout=40, headers={"User-Agent": "TemperQ-research/1.0 public-source-review", "Accept": "application/vnd.github+json"} if kind == "repo" else {"User-Agent": "TemperQ-research/1.0 public-source-review"})
        result.update(status=r.status_code, final_url=r.url, content_type=r.headers.get("Content-Type", ""))
        if not r.ok:
            result["error"] = r.text[:200]
            return result
        if kind == "repo":
            data = r.json()
            result["metadata"] = {k: data.get(k) for k in ["full_name", "html_url", "description", "archived", "default_branch", "license", "pushed_at", "updated_at"]}
            return result
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", key)
        if r.content.startswith(b"%PDF"):
            target = OUT / (safe + ".pdf")
            target.write_bytes(r.content)
            from pypdf import PdfReader
            reader = PdfReader(target)
            text = "\n".join(p.extract_text() or "" for p in reader.pages)
            result["pages"] = len(reader.pages)
        else:
            soup = BeautifulSoup(r.content, "html.parser")
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            text = soup.get_text("\n", strip=True)
        (OUT / (safe + ".txt")).write_text(text, encoding="utf-8")
        result["text_file"] = safe + ".txt"
        result["text_chars"] = len(text)
        result["preview"] = text[:450]
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [("repo", x, "https://api.github.com/repos/" + x) for x in REPOS]
    jobs += [("page", k, v) for k, v in PAGES.items()]
    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch, *job) for job in jobs]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps({k: result.get(k) for k in ["key", "status", "text_chars", "error", "metadata"]}, ensure_ascii=False), flush=True)
    (OUT / "public_sources.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
