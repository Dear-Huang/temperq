from qwb import card

MANIFEST = {"name": "m", "licenses": {"model": "Apache-2.0", "data": "recipe only"},
            "base_model": {"name": "lightgbm", "version": "4.7.0"},
            "train_config": {"seeds": [0, 1]}, "data_snapshot_id": "snap-x",
            "deviations_from_default": ["universe = 'all_ex_st' (default 'all')"]}
TASK = {"name": "t", "market": "cn_a", "template": "daily",
        "label": {"start": {"day": 1}, "end": {"day": 6}}}
STAT = {"mean": 0.1, "t": 5.0}
METRICS = {"ic": {"lgbm_ensemble": {"final_test": STAT}, "lgbm_seed0": {"final_test": STAT}},
           "final_test_years": [2024], "nw_lag": 5, "shuffle_null_mean": 0.0,
           "shuffle_null_sd": 0.001, "seed_pred_rank_corr": [0.98]}


def test_render_parse_roundtrip():
    md = card.render(MANIFEST, TASK, METRICS, {"R1": True, "R2": True})
    p = card.parse(md)
    assert p["ok"] and p["is_qwb"] and not p["flags"]
    f = p["fields"]
    assert f["base_model"] == "lightgbm" and f["seeds"] == ["0", "1"]
    assert f["reproducibility"]["R2"] == "True" and f["custom_code"] is False
    assert "lgbm_seed0" not in md.split("---")[2]      # per-seed rows kept out of the card


def test_parse_is_inert_and_sanitizes():
    hostile = ("---\nbase_model: \"<script>alert(1)</script>\"\n"
               "task: !!python/object/apply:os.system ['echo pwned']\n---\nbody")
    p = card.parse(hostile)
    assert not p["ok"] and "bad YAML" in p["error"]     # safe_load refuses python tags
    p = card.parse("---\nbase_model: \"<img src=x onerror=1>\"\n---\n")
    assert p["fields"]["base_model"].startswith("&lt;img")


def test_flags_and_missing():
    p = card.parse("---\ntask: x\n---\n年化 80% 稳赚，加微信进付费群")
    assert len(p["flags"]) >= 3 and "base_model" in p["missing"] and not p["is_qwb"]
    assert card.parse("no front matter")["ok"] is False
