"""Append-only governance reviews and explicit proposal selections."""
from __future__ import annotations

import uuid

from . import experiments as ex


def root(workdir, review_id):
    if not ex.ID_RE.fullmatch(review_id):
        raise ValueError("诊断编号无效")
    return workdir / "runs/governance_reviews" / review_id


def save(workdir, case, result):
    review_id = uuid.uuid4().hex
    directory = root(workdir, review_id)
    directory.mkdir(parents=True, exist_ok=False)
    ex.write(directory / "input.json", case.model_dump(mode="json"))
    ex.write(directory / "result.json", result)
    ex.write(directory / "record.json", {"id": review_id, "created_at": ex.now(), "name": case.name,
                                         "result_digest": result["result_digest"]})
    return review_id


def read(workdir, review_id):
    directory = root(workdir, review_id)
    record = ex.read(directory / "record.json")
    original = ex.read(directory / "input.json")
    result = ex.read(directory / "result.json")
    digest = ex.object_digest({k: v for k, v in result.items() if k != "result_digest"})
    if (record["id"] != review_id or result["schema_version"] != "governance_review_v1"
            or digest != result["result_digest"] or digest != record["result_digest"]
            or ex.object_digest(original) != result["input_digest"]):
        raise ValueError("诊断输入或结果摘要不一致，请重新诊断；原记录保留")
    return record, original, result


def history(workdir):
    records = []
    for path in (workdir / "runs/governance_reviews").glob("*/record.json"):
        try:
            record = ex.read(path)
            if ex.ID_RE.fullmatch(path.parent.name) and record["id"] == path.parent.name:
                records.append(record)
        except (ValueError, OSError, KeyError):
            continue
    return sorted(records, key=lambda r: r["created_at"], reverse=True)


def select(workdir, review_id, candidate_id, digest):
    _, _, result = read(workdir, review_id)
    if digest != result["result_digest"]:
        raise ValueError("确认的结果版本已经变化，请重新查看")
    candidate = next((c for c in result["candidates"] if c["id"] == candidate_id), None)
    if candidate is None or not candidate["feasible"]:
        raise ValueError("只能确认通过约束核对的候选方案")
    identity = uuid.uuid4().hex
    directory = root(workdir, review_id) / "selections"
    directory.mkdir(exist_ok=True)
    record = {"id": identity, "review_id": review_id, "candidate_id": candidate_id,
              "result_digest": digest, "confirmed_at": ex.now(), "scope": "saved_proposal_not_orders_or_deployment"}
    ex.write(directory / (identity + ".json"), record)
    return record


def selection(workdir, review_id, selection_id):
    if not ex.ID_RE.fullmatch(selection_id):
        raise ValueError("方案确认编号无效")
    _, _, result = read(workdir, review_id)
    record = ex.read(root(workdir, review_id) / "selections" / (selection_id + ".json"))
    if record["review_id"] != review_id or record["id"] != selection_id or record["result_digest"] != result["result_digest"]:
        raise ValueError("方案确认记录不匹配")
    candidate = next(c for c in result["candidates"] if c["id"] == record["candidate_id"])
    if not candidate["feasible"]:
        raise ValueError("此方案没有通过约束核对")
    return record, candidate
