"""Format regression from repository files only (AUDIT_EVIDENCE_SPEC R8).

Every supported platform format has a real export header (no data rows) and a synthetic
sample built from one account. Each sample must import, close its books and give the
expected counts. Unrecognised files must fail with a clear message. Runs on a fresh clone.
"""
import csv
import hashlib
import json
from pathlib import Path

import pytest

from qwb import strategy_audit as audit, strategy_imports as imports

ROOT = Path(__file__).parent / "fixtures" / "formats"
MANIFEST = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
HEADERS = json.loads((ROOT / "headers" / "headers.json").read_text(encoding="utf-8"))
FORMATS = sorted(MANIFEST["formats"])


def load(name):
    spec = MANIFEST["formats"][name]
    return [imports.SourceFile(f, (ROOT / "samples" / name / f).read_bytes()) for f in spec["files"]], spec


@pytest.mark.parametrize("name", FORMATS)
def test_sample_files_match_their_recorded_hashes(name):
    files, spec = load(name)
    for source in files:
        assert hashlib.sha256(source.data).hexdigest() == spec["files"][source.name], \
            f"{name}/{source.name} 变了：确认是有意改格式后运行 scripts/make_format_fixtures.py"


@pytest.mark.parametrize("name", FORMATS)
def test_each_format_imports_and_closes_its_books(name):
    files, spec = load(name)
    want = spec["expected"]
    bundle = imports.normalize(files, adapter="auto", name=name)
    assert bundle["adapter"] == want["adapter"]
    assert len(bundle["fills"]) == want["fills"]
    quality = bundle.get("quality") or {}
    if "inferred_dividends" in want:
        assert quality["inferred_actions"]["cash_dividend"] == want["inferred_dividends"]
    if "receivable_dividend_days" in want:
        assert quality["receivable_dividend_days"] == want["receivable_dividend_days"]
    if "orders" in want:
        assert len(bundle["orders"]) == want["orders"]
    if "rank_rows" in want:
        assert len(bundle["scores"]) == want["rank_rows"]
    result = audit.analyze(bundle)
    accounting = result["modules"]["accounting"]
    assert accounting["status"] == want["accounting"], [s for s in accounting["summary"] if s["failures"]]


def test_all_samples_describe_the_same_account():
    """Same ledger in every format: the same end equity and total fees."""
    ends, fees = set(), set()
    for name in FORMATS:
        bundle = imports.normalize(load(name)[0], adapter="auto", name=name)
        ends.add(round(bundle["snapshots"][-1]["equity"], 2))
        fees.add(round(sum(f["fee"] for f in bundle["fills"]), 2))
    assert len(ends) == 1 and len(fees) == 1, (ends, fees)


@pytest.mark.parametrize("path", sorted(HEADERS))
def test_real_headers_are_still_recognised(path):
    """Header-only files from real exports: detection must not regress, and no data rows are kept."""
    lines = (ROOT / "headers" / path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "header fixtures must not contain data"
    if path.endswith(".txt"):
        assert imports.LOG_LINE.match(lines[0])
        return
    from qwb.audit_materials import material_kind
    columns = next(csv.reader([lines[0]]))
    assert columns == HEADERS[path]["columns"]
    data = (lines[0] + "\n").encode("utf-8")
    assert material_kind(imports.SourceFile(Path(path).name, data))


UNRECOGNISED = [
    ("unknown.csv", b"a,b,c\n1,2,3\n", "未识别"),
    ("trades.xlsx", b"PK\x03\x04", "xlsx"),
    ("empty.csv", b"", "表头"),
    ("bad.zip", b"not a zip", "压缩包"),
]


@pytest.mark.parametrize("name, data, message", UNRECOGNISED)
def test_unrecognised_files_fail_with_a_clear_message(name, data, message):
    with pytest.raises(ValueError, match=message):
        imports.normalize([imports.SourceFile(name, data), imports.SourceFile("s.py", b"x = 1\n")], adapter="auto", name="x")
