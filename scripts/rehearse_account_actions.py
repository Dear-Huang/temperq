"""Run a synthetic account-action demonstration; no market-performance evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qwb.account_rehearsal import Action, RehearsalPlan, replay_actions
from qwb.portfolio.contracts import AccountState, Position, SettlementLot, TargetWeights, TASK
from qwb.portfolio.reference import ExecutionBar

D0 = "2024-01-02T15:30:00+08:00"
O1 = "2024-01-03T09:30:00+08:00"
C1 = "2024-01-03T15:30:00+08:00"
O2 = "2024-01-04T09:30:00+08:00"


def example(equity, *, locked=False):
    shares = int(equity / 20)
    position = Position("A", shares, 0 if locked else shares, 10, D0,
                        (SettlementLot(shares, O2),) if locked else ())
    state = AccountState(D0, "synthetic-not-market-data", equity / 2, (position,))
    actions = [Action("hold", None)]
    for name, weights in (("small_switch", (("A", .375), ("B", .125))),
                          ("full_switch", (("B", .5),))):
        target = TargetWeights(name, D0, O1, TASK, "frozen-synthetic-signal-v1",
                               state.data_snapshot_id, state.state_id, "A-B", "open-ioc",
                               weights, .5)
        actions.append(Action(name, target))
    plan = RehearsalPlan(state, tuple(actions), "hold", ("A", "B"), "A-B", "open-ioc", O1, C1, O2)
    market = (ExecutionBar("A", 10, 10, 11, 9), ExecutionBar("B", 10, 10.02, 11, 9))
    return replay_actions(plan, market)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = {"small_account": example(10000), "large_account": example(1000000),
               "t1_locked": example(10000, locked=True)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"synthetic_only": True, "cases": results},
                                     ensure_ascii=True, indent=2), encoding="utf-8")
    for name, report in results.items():
        for row in report["rows"]:
            print(f"{name:16} {row['action_id']:14} "
                  f"delta={row['simulated_delta_net_pnl']:10.2f} "
                  f"cost={row['total_cost']:8.2f} eligible={row['paired_label_eligible']}")
    print(f"Synthetic rehearsal only. Report: {args.output}")


if __name__ == "__main__":
    main()
