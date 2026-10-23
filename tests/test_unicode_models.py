"""Windows native LightGBM filenames cannot safely handle Chinese paths."""
import json

import numpy as np
import pytest

from qwb import consistency, daily


def test_unicode_prediction_and_packaged_consistency_match_native_model(tmp_path):
    lgb = pytest.importorskip("lightgbm")
    rng = np.random.default_rng(72)
    x = rng.normal(size=(64, 2)).astype("float32")
    model = lgb.train({"verbose": -1, "num_threads": 1, "min_data_in_leaf": 2},
                      lgb.Dataset(x, label=x[:, 0] - x[:, 1], feature_name=["x", "y"]), num_boost_round=5)
    path = tmp_path / "中文 模型" / "models"
    path.mkdir(parents=True)
    saved = path / "lgbm.txt"
    saved.write_text(model.model_to_string(), encoding="utf-8")
    expected = model.predict(x, num_threads=1)
    actual = daily.predict_rows(path, {"model": "lightgbm", "files": [saved.name]}, x)
    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-12)
    sample = consistency.make_sample([saved], ["x", "y"])
    (path.parent / "sample.json").write_text(json.dumps(sample), encoding="utf-8")
    assert consistency.check(path.parent, "sample.json")["R2"]
