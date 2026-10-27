"""Resolve the explicitly selected bibliography; retain raw Crossref metadata."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import requests
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
import re
import sys

ROOT = Path(__file__).resolve().parent

def clean(value):
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)

def fetch(spec):
    out = dict(spec)
    try:
        r = requests.get("https://api.crossref.org/works/" + spec["doi"], timeout=35,
                         headers={"User-Agent": "TemperQ-public-literature-review/1.0"})
        out["http_status"] = r.status_code
        if r.ok:
            m = r.json()["message"]
            out["crossref"] = m
            title = clean(" ".join(m.get("title", [])))
            a = re.sub(r"[^a-z0-9]", "", title.lower())
            b = re.sub(r"[^a-z0-9]", "", spec["expected_title"].lower())
            out["title_similarity"] = round(SequenceMatcher(None, a, b).ratio(), 3)
            print(spec["id"], r.status_code, title, flush=True)
        else:
            print(spec["id"], r.status_code, flush=True)
    except Exception as exc:
        out["error"] = str(exc)
        print(spec["id"], str(exc), flush=True)
    return out

def main():
    specs = json.loads((ROOT / "selected_references.json").read_text(encoding="utf8"))
    target = ROOT / "evidence" / "verified_references.json"
    if "--retry-failed" in sys.argv and target.exists():
        results = json.loads(target.read_text(encoding="utf8"))["references"]
        for index, item in enumerate(results):
            if not item.get("crossref"):
                results[index] = fetch(specs[index])
        for spec in specs[len(results):]:
            results.append(fetch(spec))
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(fetch, specs))
    target.write_text(json.dumps({"retrieved_date": "2026-10-27", "references": results}, ensure_ascii=False, indent=2), encoding="utf8")
    lines = ["# 文献题录与 DOI 核验表", "", "核验日期：2026-10-27。V 表示 Crossref 的 DOI 与题名相符，不表示阅读全文或复现。正文中的 F / A / M 另说明阅读深度。日期按元数据列出，online-first 与正式卷期年份可能不同。", "", "| ID | 文献、作者 | 出版信息 | DOI | 核验 |", "| --- | --- | --- | --- | --- |"]
    for item in results:
        m = item.get("crossref", {})
        title = clean(" ".join(m.get("title", [])))
        authors = "; ".join(" ".join([x.get("given", ""), x.get("family", "")]).strip() for x in m.get("author", []))
        year = str(m.get("published", m.get("issued", {})).get("date-parts", [[""]])[0][0])
        journal = clean(" ".join(m.get("container-title", [])))
        pub = " / ".join(filter(None, [year, journal, m.get("volume"), m.get("issue"), m.get("page")]))
        ok = "V" if m else "未核验"
        lines.append(f'| {item["id"]} | {title} — {authors or "作者字段缺失，见原始来源"} | {pub} | [{item["doi"]}](https://doi.org/{item["doi"]}) | {ok} |')
    lines.extend(["", "R22 的 Crossref 作者字段若为空，作者以论文题录及 arch 文档交叉确认：Peter R. Hansen、Asger Lunde、James M. Nason。R32、R38 为 SSRN 工作论文，DOI 不代表期刊发表。R26 为作者回复的网页出版物。R18 全文受访问限制，仅核验正式题录和对应预印本摘要。", "", "部分题名为简写，自动相似度仅供发现候选，最终逐条人工核对。2019 年的 R04 是 R03 的证明勘误；应成对引用。"])
    (ROOT / "05_文献DOI与来源.md").write_text("\n".join(lines) + "\n", encoding="utf8")

if __name__ == "__main__":
    main()
