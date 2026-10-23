"""Actual fitting, historical selection evidence, and safe checkpoint replay."""

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from qwb.training.sequence import GRUConfig, GRUPredictor

CONTEXT = {"fit_cutoff": "2024-01-01T15:30:00+08:00",
           "selection_cutoff": "2024-02-01T15:30:00+08:00",
           "max_train_label_available": "2023-12-29T15:00:00+08:00",
           "max_validation_label_available": "2024-01-31T15:00:00+08:00"}


def data():
    rng = np.random.default_rng(5)
    x = rng.normal(0, .02, (80, 10, 6)).astype(np.float32)
    valid = np.ones_like(x, bool)
    y = .1 * x[:, -1, 3]
    return x, valid, y


def fit(model):
    x, valid, y = data()
    record = model.fit(x[:60], valid[:60], y[:60], x[60:], valid[60:], y[60:], context=CONTEXT)
    return x, valid, record


def test_same_seed_fit_and_checkpoint_predictions_are_reproducible(tmp_path):
    config = GRUConfig(hidden_size=8, epochs=3, batch_size=20, seed=2)
    first, second = GRUPredictor(config), GRUPredictor(config)
    x, valid, record = fit(first)
    fit(second)
    np.testing.assert_array_equal(first.predict(x, valid), second.predict(x, valid))
    assert record["n_train"] == 60 and 1 <= record["best_epoch"] <= 3
    assert not record["refit_on_validation"]
    labels = data()[2]
    assert record["label_transform"]["mean"] == pytest.approx(labels[:60].mean(), abs=1e-8)
    assert record["label_transform"]["scale"] == pytest.approx(labels[:60].std(), rel=1e-6)
    path = tmp_path / "model"
    first.save(path)
    restored = GRUPredictor.load(path)
    np.testing.assert_array_equal(first.predict(x, valid), restored.predict(x, valid))
    with pytest.raises(FileExistsError):
        first.save(path)
    (path / "weights.pt").write_bytes(b"modified")
    with pytest.raises(ValueError, match="modified"):
        GRUPredictor.load(path)


def test_encoder_can_be_frozen_without_changing_its_parameters():
    model = GRUPredictor(GRUConfig(hidden_size=8, epochs=2, freeze_encoder=True))
    original = {k: p.clone() for k, p in model.net.encoder.state_dict().items()}
    fit(model)
    for k, p in model.net.encoder.state_dict().items():
        assert torch.equal(p, original[k])


def test_masked_values_cannot_affect_network_output():
    model = GRUPredictor(GRUConfig(hidden_size=4))
    values = torch.zeros(3, 5, 6)
    valid = torch.ones_like(values, dtype=torch.bool)
    valid[:, :2] = False
    changed = values.clone()
    changed[:, :2] = 100
    with torch.no_grad():
        assert torch.equal(model.net(values, valid), model.net(changed, valid))


def test_training_refuses_unavailable_labels_and_unobserved_samples():
    x, valid, y = data()
    model = GRUPredictor(GRUConfig(hidden_size=8, epochs=1))
    future = dict(CONTEXT, max_train_label_available="2024-01-03T15:30:00+08:00")
    with pytest.raises(ValueError, match="unavailable"):
        model.fit(x[:60], valid[:60], y[:60], x[60:], valid[60:], y[60:], context=future)
    valid[0] = False
    with pytest.raises(ValueError, match="observed"):
        model.fit(x[:60], valid[:60], y[:60], x[60:], valid[60:], y[60:], context=CONTEXT)
