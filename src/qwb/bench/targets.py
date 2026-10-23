"""Independent, data-only target-weight submission replay and arithmetic audit."""

from __future__ import annotations

from dataclasses import dataclass, replace
import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .. import experiments
from ..portfolio.contracts import (AccountState, TargetWeights, TASK, identity, number,
                                   timestamp)
from ..portfolio.reference import ExecutionBar, Fees, execute_session
from ..portfolio.lifecycle import (LifecycleAccountState, LifecycleTargetWeights, Terminal,
                                   execute_lifecycle_session, parse_actions, phases, validate_initial)


def read(path):
    def reject(value):
        raise ValueError(f"nonstandard JSON number: {value}")
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject)


def fields(value, expected, name):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f"{name} fields must be exactly {sorted(expected)}")
    return value


@dataclass(frozen=True)
class DecisionContext:
    decision_id: str
    state: AccountState
    eligible_assets: tuple[str, ...]
    assets: tuple[str, ...]
    universe_id: str
    protocol_id: str
    execution_not_before: str
    max_weight: float
    min_cash: float
    known_actions: tuple = ()


def _state(raw):
    if not isinstance(raw, dict):
        raise ValueError("initial account must be an object")
    cls = LifecycleAccountState if raw.get("schema_version") == "account_state_v2" else AccountState
    return cls.from_dict(raw)


def validate_protocol(protocol):
    fields(protocol, {"schema_version", "task", "initial_state", "fees", "constraints", "sessions",
                      "corporate_actions", "data_kind", "data_license", "execution_model"}, "target protocol")
    lifecycle = protocol["schema_version"] == "target_protocol_v2"
    if protocol["schema_version"] not in {"target_protocol_v1", "target_protocol_v2"} or protocol["task"] != TASK:
        raise ValueError("unsupported target protocol or task")
    if protocol["execution_model"] != ("raw_share_lifecycle_v2" if lifecycle else "raw_share_open_ioc_v1"):
        raise ValueError("unsupported target execution model")
    if not lifecycle and protocol["corporate_actions"] != []:
        raise ValueError("this reference requires explicit empty corporate actions; nonempty events are unsupported")
    if protocol["data_kind"] not in {"synthetic", "market_development"}:
        raise ValueError("target data kind must be synthetic or market_development")
    identity(protocol["data_license"], "data license declaration")
    state = _state(protocol["initial_state"])
    if lifecycle != isinstance(state, LifecycleAccountState):
        raise ValueError("protocol and initial account schema mismatch")
    actions = parse_actions(protocol["corporate_actions"]) if lifecycle else ()
    if lifecycle:
        validate_initial(state, actions)
    if state.pending_orders:
        raise ValueError("open IOC protocol cannot consume existing pending orders")
    Fees(**fields(protocol["fees"], Fees.__dataclass_fields__, "fees"))
    limits = fields(protocol["constraints"], {"max_weight", "min_cash"}, "constraints")
    number(limits["max_weight"], "max weight", lower=1e-8, upper=1)
    number(limits["min_cash"], "min cash", upper=1)
    sessions = protocol["sessions"]
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("target protocol needs nonempty explicit sessions")
    previous = None
    ids = set()
    for session in sessions:
        fields(session, {"decision_id", "decision_time", "open_at", "close_at", "next_open_at",
                         "eligible_assets", "bars"}, "target session")
        identity(session["decision_id"], "decision id")
        if session["decision_id"] in ids:
            raise ValueError("duplicate protocol decision")
        ids.add(session["decision_id"])
        decision, opened, closed, next_open = [timestamp(session[k]) for k in
                                              ("decision_time", "open_at", "close_at", "next_open_at")]
        if not decision < opened < closed < next_open:
            raise ValueError("session timestamps must follow decision/open/close/next open order")
        local = [stamp.tz_convert("Asia/Shanghai") for stamp in (decision, opened, closed, next_open)]
        if ([stamp.strftime("%H:%M:%S") for stamp in local] != ["15:30:00", "09:30:00", "15:00:00", "09:30:00"]
                or any(stamp.microsecond or stamp.nanosecond for stamp in local)
                or local[1].date() != local[2].date() or local[0].date() >= local[1].date()
                or local[2].date() >= local[3].date()):
            raise ValueError("target task requires close decision and next-session open/close times")
        if previous is None:
            if decision != timestamp(state.observed_at):
                raise ValueError("initial account must be observed at the first decision")
        elif (opened != timestamp(previous["next_open_at"])
              or local[0].date() != timestamp(previous["close_at"]).tz_convert("Asia/Shanghai").date()):
            raise ValueError("target sessions must form a continuous declared trading path")
        eligible = session["eligible_assets"]
        if not isinstance(eligible, list) or len(set(eligible)) != len(eligible):
            raise ValueError("eligible assets must be an explicit unique list")
        for code in eligible:
            identity(code, "eligible asset")
        if not isinstance(session["bars"], list):
            raise ValueError("execution bars must be an explicit list")
        codes = []
        for raw in session["bars"]:
            fields(raw, set(ExecutionBar.__dataclass_fields__) - {"buy_allowed"}, "execution bar")
            codes.append(ExecutionBar(**raw).asset_id)
        if len(set(codes)) != len(codes) or set(eligible) - set(codes):
            raise ValueError("duplicate execution rows or missing eligible execution rows")
        previous = session
    if lifecycle:
        # This daily model has pre-open events and close-of-session ownership
        # records. Other intraday event timing requires a new execution model.
        initial, final = timestamp(state.observed_at), timestamp(sessions[-1]["close_at"])
        preopen = {timestamp(s["open_at"]).tz_convert("Asia/Shanghai").normalize() + pd.Timedelta(hours=9)
                   for s in sessions}
        closes = {timestamp(s["close_at"]) for s in sessions}
        for at, _, phase, _ in phases(actions):
            if initial < at <= final and at not in (closes if phase == "record" else preopen):
                raise ValueError("corporate phases require declared session 09:00 pre-open or 15:00 record time")
        terminal_by_asset = {}
        for action in actions:
            if isinstance(action, Terminal):
                if action.asset_id in terminal_by_asset:
                    raise ValueError("duplicate terminal action for asset")
                terminal_by_asset[action.asset_id] = timestamp(action.effective_at)
        for session in sessions:
            if any(code in terminal_by_asset and terminal_by_asset[code] <= timestamp(session["decision_time"])
                   for code in session["eligible_assets"]):
                raise ValueError("terminal assets cannot remain eligible for new decisions")
    return experiments.object_digest(protocol)


def _failure(raw, context):
    keys = {"schema_version", "decision_id", "decision_time", "task", "model_version", "data_snapshot_id",
            "state_id", "universe_id", "protocol_id", "reason"}
    fields(raw, keys, "decision failure")
    for key in keys:
        identity(raw[key], key)
    if not (raw["schema_version"] == "decision_failure_v1" and raw["decision_id"] == context.decision_id
            and timestamp(raw["decision_time"]) == timestamp(context.state.observed_at)
            and raw["state_id"] == context.state.state_id and raw["task"] == TASK
            and raw["data_snapshot_id"] == context.state.data_snapshot_id
            and raw["universe_id"] == context.universe_id and raw["protocol_id"] == context.protocol_id):
        raise ValueError("decision failure identity mismatch")


def replay(protocol, policy):
    """Trusted local policy sees decision context; CLI never imports submission code."""
    protocol_id = validate_protocol(protocol)
    state = _state(protocol["initial_state"])
    lifecycle = isinstance(state, LifecycleAccountState)
    corporate_actions = parse_actions(protocol["corporate_actions"]) if lifecycle else ()
    fees, limits = Fees(**protocol["fees"]), protocol["constraints"]
    audits, states, actions, versions = [], [], [], set()
    initial_equity = state.equity
    for session in protocol["sessions"]:
        if timestamp(state.observed_at) != timestamp(session["decision_time"]):
            state = replace(state, observed_at=session["decision_time"])
        assets = tuple(sorted(set(session["eligible_assets"]) | {p.asset_id for p in state.positions}))
        if not assets:
            raise ValueError("empty decision universe: include a declared asset even for a cash strategy")
        universe_id = experiments.object_digest({"decision_time": str(timestamp(session["decision_time"])),
                                                 "eligible": sorted(session["eligible_assets"]), "assets": assets})
        context = DecisionContext(session["decision_id"], state, tuple(sorted(session["eligible_assets"])),
                                  assets, universe_id, protocol_id, session["open_at"], **limits,
                                  known_actions=tuple(a for a in corporate_actions if
                                                     timestamp(a.available_at) <= timestamp(state.observed_at)))
        action = policy(context)
        if isinstance(action, TargetWeights):
            action = action.to_dict()
        if not isinstance(action, dict):
            raise ValueError("every decision requires a target or explicit failure record")
        if action.get("schema_version") == "decision_failure_v1":
            _failure(action, context)
            target = None
        else:
            target_cls = LifecycleTargetWeights if action.get("schema_version") == "target_weights_v2" else TargetWeights
            target = target_cls.from_dict(action)
            if target.decision_id != context.decision_id:
                raise ValueError("target decision id mismatch")
        versions.add(action["model_version"])
        if len(versions) != 1:
            raise ValueError("one model version required per target submission")
        # Future execution rows never determine the decision-known allocation universe.
        rows = {row["asset_id"]: row for row in session["bars"]}
        if set(assets) - set(rows):
            raise ValueError("held assets require explicit execution rows, even with missing prices")
        bars = tuple(ExecutionBar(**rows[code], buy_allowed=code in context.eligible_assets) for code in assets)
        before = state
        executor = execute_lifecycle_session if lifecycle else execute_session
        extra = {"actions": corporate_actions} if lifecycle else {}
        state, audit = executor(state, target, bars, open_at=session["open_at"], close_at=session["close_at"],
            next_open_at=session["next_open_at"], decision_assets=assets, universe_id=universe_id,
            protocol_id=protocol_id, fees=fees, **limits, **extra)
        audit.update(decision_id=context.decision_id, decision_time=str(before.observed_at),
                     target_weights=dict(target.weights) if target else None,
                     target_cash_weight=target.cash_weight if target else None,
                     decision_status="target" if target else "explicit_failure_keep_holdings",
                     net_return=state.equity / before.equity - 1)
        if lifecycle:
            audit["target_receivable_weight"] = target.receivable_weight if target else None
        audits.append(audit)
        states.append({"before": before.to_dict(), "after": state.to_dict()})
        actions.append(copy.deepcopy(action))
    equity = np.array([initial_equity, *[row["equity_close"] for row in audits]])
    return {"schema_version": "target_replay_v2" if lifecycle else "target_replay_v1", "protocol_id": protocol_id,
            "model_version": next(iter(versions)), "data_snapshot_id": state.data_snapshot_id,
            "evidence_status": "SYNTHETIC_ONLY" if protocol["data_kind"] == "synthetic" else "DIAGNOSTIC_ONLY",
            "economic_efficacy": "NOT_ESTABLISHED", "initial_equity": initial_equity,
            "final_equity": state.equity, "net_return": state.equity / initial_equity - 1,
            "mean_log_growth": float(np.diff(np.log(equity)).mean()),
            "max_drawdown": float(np.min(equity / np.maximum.accumulate(equity) - 1)),
            "total_cost": sum(row["total_cost"] for row in audits),
            "explicit_failures": sum(row["decision_status"] != "target" for row in audits),
            "unfilled_quantity": sum(fill["unfilled_quantity"] for row in audits for fill in row["fills"]),
            "audits": audits, "states": states, "decisions": actions,
            "limitations": (["declared_withholding_only", "fractional_split_rejected"] if lifecycle else ["no_corporate_actions"])
                            + ["open_bar_proxy", "no_existing_pending_orders", "no_capacity_certification", "policy_causality_not_attested"]}


def replay_submission(protocol, submission):
    protocol_id = validate_protocol(protocol)
    fields(submission, {"schema_version", "protocol_id", "decisions"}, "target submission")
    schema = "target_submission_v2" if protocol["schema_version"] == "target_protocol_v2" else "target_submission_v1"
    if submission["schema_version"] != schema or submission["protocol_id"] != protocol_id:
        raise ValueError("target submission protocol or schema mismatch")
    decisions = submission["decisions"]
    if not isinstance(decisions, list) or len(decisions) != len(protocol["sessions"]):
        raise ValueError("target submission must cover every declared decision exactly once")
    by_id = {}
    for row in decisions:
        if not isinstance(row, dict) or row.get("decision_id") in by_id:
            raise ValueError("invalid or duplicate target decision")
        by_id[row.get("decision_id")] = row
    if set(by_id) != {s["decision_id"] for s in protocol["sessions"]}:
        raise ValueError("missing or unknown target decisions")
    return replay(protocol, lambda context: by_id[context.decision_id])


def evaluate(protocol_path, submission_path, output):
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    sources = {"protocol.json": Path(protocol_path).resolve(), "submission.json": Path(submission_path).resolve()}
    try:
        inputs = {name: {"path": str(path), "hash": experiments.digest(path)} for name, path in sources.items()}
        experiments.write(root / "inputs.json", inputs)
        for name, source in sources.items():
            (root / name).write_bytes(source.read_bytes())
            if experiments.digest(root / name) != inputs[name]["hash"]:
                raise ValueError("target input changed while freezing")
        result = replay_submission(read(root / "protocol.json"), read(root / "submission.json"))
        for name, source in sources.items():
            if experiments.digest(source) != inputs[name]["hash"]:
                raise ValueError("target input changed during replay")
        experiments.write(root / "replay.json", result)
        score = _score(result)
        lines = ["# Target-weight account replay", "", f"Evidence: **{result['evidence_status']}**. "
                 "Economic efficacy: **NOT_ESTABLISHED**.", "",
                 f"Initial equity: {result['initial_equity']:.6f}; final equity: {result['final_equity']:.6f}; "
                 f"total cost: {result['total_cost']:.6f}.", "",
                 "| Decision | Close equity | Cost | Status | Unfilled shares |",
                 "| --- | ---: | ---: | --- | ---: |"]
        for row in result["audits"]:
            lines.append(f"| {row['decision_id']} | {row['equity_close']:.6f} | {row['total_cost']:.6f} | "
                         f"{row['decision_status']} | {sum(fill['unfilled_quantity'] for fill in row['fills'])} |")
        lines += ["", "Actual raw shares, settlement, cash, fills, intended/actual weights and residuals are in replay.json.",
                  "", "Limitations: " + ", ".join(result["limitations"]) + ".",
                  "No market capacity, policy causality or investment performance is certified."]
        (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        score = {"schema_version": "target_scorecard_v1", "status": "INVALID",
                 "implementation_hash": experiments.code_identity(), "error": f"{type(exc).__name__}: {exc}"}
    experiments.write(root / "scorecard.json", score)
    experiments.write(root / "checksums.json", experiments.inventory(root))
    return score


def _score(result):
    return {"schema_version": "target_scorecard_v1", "status": "COMPLETED",
            "protocol_id": result["protocol_id"], "implementation_hash": experiments.code_identity(),
            "evidence_status": result["evidence_status"], "economic_efficacy": "NOT_ESTABLISHED",
            "summary": {key: result[key] for key in ("initial_equity", "final_equity", "net_return",
                        "max_drawdown", "mean_log_growth", "total_cost", "explicit_failures", "unfilled_quantity")}}


def verify(output, *, inputs=False):
    root = Path(output).resolve()
    expected = read(root / "checksums.json")
    actual = experiments.inventory(root)
    actual.pop("checksums.json", None)
    if actual != expected:
        raise ValueError("target artifacts changed")
    score = read(root / "scorecard.json")
    if score["status"] != "COMPLETED":
        raise ValueError("target result is not a completed replay")
    if score["implementation_hash"] != experiments.code_identity():
        raise ValueError("target arithmetic audit needs its recorded implementation")
    result = replay_submission(read(root / "protocol.json"), read(root / "submission.json"))
    if experiments.object_digest(result) != experiments.object_digest(read(root / "replay.json")):
        raise ValueError("target account arithmetic mismatch")
    if score != _score(result):
        raise ValueError("target scorecard arithmetic mismatch")
    if inputs:
        for entry in read(root / "inputs.json").values():
            if experiments.digest(Path(entry["path"])) != entry["hash"]:
                raise ValueError("target original input changed")
    return {"status": "PASS", "scope": "frozen_target_replay_arithmetic", "inputs_checked": inputs,
            "evidence_status": result["evidence_status"], "economic_efficacy": "NOT_ESTABLISHED"}


def create_fixture(directory, *, lifecycle=False):
    from dataclasses import asdict

    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    sessions = []
    for i in range(3):
        opened = [10.0, 11.0, 10.5][i]
        sessions.append({"decision_id": f"d{i}", "decision_time": days[i] + "T15:30:00+08:00",
                         "open_at": days[i + 1] + "T09:30:00+08:00",
                         "close_at": days[i + 1] + "T15:00:00+08:00",
                         "next_open_at": days[i + 2] + "T09:30:00+08:00", "eligible_assets": ["A"],
                         "bars": [asdict(ExecutionBar("A", opened, [11.0, 10.5, 11.0][i], 20.0, 1.0, sell_lot=100))]})
        sessions[-1]["bars"][0].pop("buy_allowed")
    protocol = {"schema_version": "target_protocol_v1", "task": TASK,
                "initial_state": AccountState(days[0] + "T15:30:00+08:00", "snap-synthetic-targets", 10000.0, ()).to_dict(),
                "fees": asdict(Fees()), "constraints": {"max_weight": 1.0, "min_cash": .05},
                "sessions": sessions, "corporate_actions": [], "data_kind": "synthetic",
                "data_license": "CC0-1.0 generated accounting fixture", "execution_model": "raw_share_open_ioc_v1"}
    if lifecycle:
        from ..portfolio.lifecycle import Dividend, Split

        ex = days[2] + "T09:00:00+08:00"
        protocol.update(schema_version="target_protocol_v2", execution_model="raw_share_lifecycle_v2",
            initial_state=LifecycleAccountState(days[0] + "T15:30:00+08:00", "snap-synthetic-targets", 10000.0).to_dict(),
            corporate_actions=[asdict(Dividend("div-A", "A", days[0] + "T15:30:00+08:00",
                sessions[0]["close_at"], ex, days[3] + "T09:00:00+08:00", 1.0, 0.0, .5, "tax_exempt")),
                asdict(Split("split-A", "A", days[0] + "T15:30:00+08:00", ex, 2, 1, "reject"))])
        for session, opened, closed in zip(sessions[1:], [5.0, 5.2], [5.2, 5.3]):
            session["bars"][0].update(open=opened, close=closed)
    def policy(context):
        cls = LifecycleTargetWeights if lifecycle else TargetWeights
        claim = context.state.receivable_equity / context.state.equity
        extra = {"receivable_weight": claim} if lifecycle else {}
        return cls(context.decision_id, context.state.observed_at, context.execution_not_before,
                             TASK, "fixed-fixture-policy-v1", context.state.data_snapshot_id, context.state.state_id,
                             context.universe_id, context.protocol_id, (("A", .9 * (1 - claim)),), .1 * (1 - claim), **extra)
    result = replay(protocol, policy)
    experiments.write(root / "protocol.json", protocol)
    experiments.write(root / "submission.json", {"schema_version": "target_submission_v2" if lifecycle else "target_submission_v1",
                      "protocol_id": result["protocol_id"], "decisions": result["decisions"]})
    return root
