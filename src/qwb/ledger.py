"""Local run ledger (guardrails: "picked the best result", "looked at the test set again").

Every run appends one line to <workdir>/runs/ledger.jsonl. Before appending,
the runs on the same task and data snapshot are read back, so the report can
say how many times the final test period has been scored, by how many
configurations, and list all of them. The ledger is a local file: it supports
honest self-reporting, it cannot prove that nothing was deleted (SPEC §14).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue                         # a torn line must not hide the rest
    return out


def append(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def history(path: Path, task: str, snapshot: str, final_years: list[int],
            config_hash: str, ic: float) -> dict:
    """Summary of earlier scorings of the same test period, plus this run."""
    same = [e for e in read(path) if e.get("task") == task and e.get("snapshot") == snapshot]
    first_years = same[0]["final_test_years"] if same else list(final_years)
    overlap = [e for e in same if set(e.get("final_test_years", [])) & set(final_years)]
    configs = {e["config_hash"] for e in overlap} | {config_hash}
    ics = [e["final_ic"] for e in overlap if e.get("final_ic") is not None] + [ic]
    return {
        "views": len(overlap) + 1,
        "configs": len(configs),
        "first_final_years": first_years,
        "years_changed": list(first_years) != list(final_years),
        "is_best": len(ics) > 1 and ic >= max(ics),
        "earlier": overlap[-20:],
        "earlier_total": len(overlap),
    }
