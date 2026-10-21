from pathlib import Path

import pytest
import yaml

from qwb import cli, runconfig

EX = Path(__file__).resolve().parents[1] / "examples"


def resolve(**over):
    raw = runconfig.load(EX / "run_smoke.yaml")
    for k, v in over.items():
        raw[k] = v
    return runconfig.resolve(raw, EX)


def test_example_configs_resolve():
    for f in ("run_smoke.yaml", "run_cn_rank_5d.yaml"):
        rc, rep = runconfig.resolve(runconfig.load(EX / f), EX)
        assert rc is not None, str(rep)
    rc, _ = runconfig.resolve(runconfig.load(EX / "run_cn_rank_5d.yaml"), EX)
    assert rc["deviations"] == ["universe = 'all_ex_st' (default 'all')"]


def test_unknown_keys_are_errors_not_defaults():
    rc, rep = resolve(train={"univers": "all"})
    assert rc is None and "UNKNOWN_KEY" in rep.codes("ERROR")
    rc, rep = resolve(model={"name": "lightgbm", "num_boost_rounds": 10})
    assert rc is None and "UNKNOWN_KEY" in rep.codes("ERROR")


def test_bad_values():
    assert "BAD_MODEL" in resolve(model={"name": "xgboost"})[1].codes()
    assert "BAD_SEEDS" in resolve(train={"seeds": []})[1].codes()
    assert "BAD_UNIVERSE" in resolve(train={"universe": "hs300"})[1].codes()
    assert "NO_TASK" in resolve(task="missing.yaml")[1].codes()


def test_lgbm_params_merge_and_show_as_deviation():
    rc, rep = resolve(model={"name": "lightgbm", "params": {"num_leaves": 31, "lamda_l2": 2}})
    assert rc["cfg"]["lgbm"]["num_leaves"] == 31
    assert rc["cfg"]["lgbm"]["learning_rate"] == 0.05            # untouched default kept
    assert any("lgbm.num_leaves" in d for d in rc["deviations"])
    assert "UNCHECKED_PARAM" in rep.codes("WARN")                 # typo of lambda_l2 flagged


def test_inline_task_and_hash_changes_with_settings():
    card = yaml.safe_load((EX / "cn_rank_5d.yaml").read_text(encoding="utf-8"))
    a, _ = resolve(task=card)
    b, _ = resolve(task=card, model={"name": "lightgbm", "num_boost_round": 60})
    assert a is not None and a["config_hash"] != b["config_hash"]


def test_cli_run_smoke_end_to_end(tmp_path, monkeypatch):
    pytest.importorskip("lightgbm")
    cfg = runconfig.load(EX / "run_smoke.yaml")
    cfg.update(task=str(EX / "cn_rank_5d.yaml"), output="out",
               train=dict(cfg["train"], seeds=[0, 1], shuffle_null_draws=2))
    p = tmp_path / "run.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", str(p)]) == 0
    out = tmp_path / "out"
    for f in ("report.md", "metrics.json", "predictions.parquet", "folds.json",
              "config.resolved.yaml", "task.yaml"):
        assert (out / f).exists(), f
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "num_boost_round = 50" in report and "合成数据" in report
    assert cli.main(["run", str(p), "--check"]) == 0
