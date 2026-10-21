import copy
from pathlib import Path

import yaml

from qwb import package, recipe, taskcard

EX = Path(__file__).resolve().parents[1] / "examples"


def test_example_task_card_ok():
    card = taskcard.load(EX / "st_task.yaml")
    rep = taskcard.validate(card)
    assert rep.ok and not rep.issues, str(rep)
    assert taskcard.derive_purge_embargo(card) == {"purge_days": 6, "embargo_days": 6}


def test_label_window_containing_signal_is_rejected():
    card = taskcard.load(EX / "st_task.yaml")
    bad = copy.deepcopy(card)
    bad["label"]["start"] = {"day": 0, "time": "09:30"}   # open[t] -> contains signal day
    assert "LABEL_BEFORE_EXECUTION" in taskcard.validate(bad).codes()


def test_signal_after_decision_rejected():
    card = taskcard.load(EX / "st_task.yaml")
    card["timing"]["signal_available"] = {"day": 1, "time": "09:00"}
    assert "SIGNAL_AFTER_DECISION" in taskcard.validate(card).codes()


def test_example_recipe_ok_and_lookahead_universe():
    r = recipe.load(EX / "st_recipe.yaml")
    assert recipe.validate(r).ok
    r["universe"]["point_in_time"] = False
    assert "LOOKAHEAD_UNIVERSE" in recipe.validate(r).codes("ERROR")
    r["experimental"] = True
    assert recipe.validate(r).ok


def test_recipe_requires_market():
    r = recipe.load(EX / "st_recipe.yaml")
    del r["market"]
    assert "MISSING_FIELD" in recipe.validate(r).codes()


def manifest():
    return yaml.safe_load((EX / "qwb_package.yaml").read_text(encoding="utf-8"))


def test_manifest_example_ok():
    rep = package.validate_manifest(manifest())
    assert rep.ok and not rep.issues, str(rep)


def test_manifest_missing_license_and_seeds():
    m = manifest()
    del m["train_config"]["seeds"]
    del m["licenses"]["data"]
    assert "MISSING_FIELD" in package.validate_manifest(m).codes()


def test_code_needs_custom_code_flag():
    m = manifest()
    m["files"].append("features.py")
    assert "UNDECLARED_CODE" in package.validate_manifest(m).codes("ERROR")
    m["custom_code"] = True
    rep = package.validate_manifest(m)
    assert rep.ok and "CUSTOM_CODE" in rep.codes("WARN")


def test_pickle_weights_warn_and_bad_paths():
    m = manifest()
    m["files"] += ["model.pkl", "../secret.txt"]
    rep = package.validate_manifest(m)
    assert "UNSAFE_WEIGHT_FORMAT" in rep.codes("WARN")
    assert "BAD_PATH" in rep.codes("ERROR")


def _make_src(root: Path) -> None:
    m = manifest()
    (root / package.MANIFEST).write_text(yaml.safe_dump(m), encoding="utf-8")
    for f in m["files"]:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")


def test_build_copies_only_whitelisted_files(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    _make_src(src)
    (src / ".env").write_text("TUSHARE_TOKEN=" + "a" * 40, encoding="utf-8")
    (src / "prices.parquet").write_bytes(b"0" * 2_000_000)
    rep = package.build(src, dest)
    assert rep.ok, str(rep)
    assert not (dest / ".env").exists() and not (dest / "prices.parquet").exists()
    assert (dest / "report" / "index.html").exists()


def test_build_catches_secret_inside_listed_file(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    _make_src(src)
    (src / "recipe.yaml").write_text("token: " + "b" * 40, encoding="utf-8")
    assert "SECRET_ASSIGNED_SECRET" in package.build(src, dest).codes("ERROR")


def test_scan_flags_raw_data(tmp_path):
    _make_src(tmp_path)
    (tmp_path / "prices.parquet").write_bytes(b"0" * 2_000_000)
    assert "RAW_DATA_IN_PACKAGE" in package.scan_dir(tmp_path).codes("ERROR")
