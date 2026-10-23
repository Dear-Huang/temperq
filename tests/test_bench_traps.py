"""Every trap pair is caught, and no clean case raises an alarm."""

import pytest

from qwb.bench import traps


@pytest.mark.parametrize("name", list(traps.PAIRS))
def test_pair_caught_without_false_alarm(name):
    case = traps.PAIRS[name]
    assert case(True), f"{name}: planted mistake was not caught"
    assert not case(False), f"{name}: clean case raised a false alarm"


def test_summary_counts():
    s = traps.run()
    assert s["n"] == len(traps.PAIRS) and s["all_pass"]
    assert s["catch_rate"] == 1.0 and s["false_alarm_rate"] == 0.0
