"""Append-only imported materials and versioned reports, separate from live ledgers."""
from __future__ import annotations

from hashlib import sha256
import uuid

from . import experiments as ex
from .strategy_imports import SCHEMA
from .strategy_audit import VERSION


def root(workdir, identity):
    if not ex.ID_RE.fullmatch(identity):
        raise ValueError("导入编号无效")
    return workdir / "runs/strategy_audits" / identity


def save(workdir, bundle, files, *, parent=None):
    prior = read(workdir, parent)[0] if parent else None
    identity = uuid.uuid4().hex
    directory = root(workdir, identity)
    directory.mkdir(parents=True, exist_ok=False)
    manifest = []
    for index, source in enumerate(files):
        stored = f"source-{index}.bin"
        (directory / stored).write_bytes(source.data)
        manifest.append({"stored": stored, "sha256": sha256(source.data).hexdigest()})
    ex.write(directory / "input.json", bundle)
    ex.write(directory / "record.json", {"id": identity, "name": bundle["name"], "created_at": ex.now(),
        "input_digest": ex.object_digest(bundle), "manifest": manifest,
        'platform': {'bigquant_v1': 'BigQuant', 'joinquant_v1': '聚宽', 'ricequant_v1': '米筐', 'generic_v1': '其他', 'code_only_v1': '未识别平台'}.get(bundle['adapter'], '其他'),
        'version': prior.get('version', 1) + 1 if prior else 1,
        'parent_id': parent, 'strategy_id': prior.get('strategy_id', parent) if prior else identity})
    return identity


def read(workdir, identity):
    directory = root(workdir, identity)
    record = ex.read(directory / "record.json")
    bundle = ex.read(directory / "input.json")
    if record["id"] != identity or bundle["schema_version"] != SCHEMA or ex.object_digest(bundle) != record["input_digest"]:
        raise ValueError("导入材料摘要不一致，原记录保留")
    manifest = record["manifest"]
    if len(manifest) != len(bundle["sources"]):
        raise ValueError("原始文件清单不一致")
    for index, item in enumerate(manifest):
        if item["stored"] != f"source-{index}.bin":
            raise ValueError("原始文件清单无效")
        digest = sha256((directory / item["stored"]).read_bytes()).hexdigest()
        if digest != item["sha256"] or digest != bundle["sources"][index]["sha256"]:
            raise ValueError("原始文件摘要不一致")
    return record, bundle


def save_report(workdir, identity, result):
    _, bundle = read(workdir, identity)
    if result["input_digest"] != ex.object_digest(bundle):
        raise ValueError("报告与导入材料不匹配")
    report_id = uuid.uuid4().hex
    directory = root(workdir, identity) / "reports" / report_id
    directory.mkdir(parents=True, exist_ok=False)
    ex.write(directory / "result.json", result)
    return report_id


def report(workdir, identity, report_id):
    record, bundle = read(workdir, identity)
    if not ex.ID_RE.fullmatch(report_id):
        raise ValueError("报告编号无效")
    result = ex.read(root(workdir, identity) / "reports" / report_id / "result.json")
    digest = ex.object_digest({k: v for k, v in result.items() if k != "result_digest"})
    if (result["schema_version"] != VERSION or result["input_digest"] != record["input_digest"]
            or result["result_digest"] != digest):
        raise ValueError("报告摘要核对失败")
    return record, bundle, result


def save_code_candidate(workdir, identity, report_id, candidate):
    _, bundle, result = report(workdir, identity, report_id)
    digest = ex.object_digest({k: v for k, v in candidate.items() if k != 'candidate_digest'})
    if (candidate['input_digest'] != ex.object_digest(bundle) or candidate['report_digest'] != result['result_digest']
            or digest != candidate['candidate_digest']):
        raise ValueError('代码候选与原材料或审计报告不一致')
    candidate_id = uuid.uuid4().hex
    directory = root(workdir, identity) / 'reports' / report_id / 'code_candidates' / candidate_id
    directory.mkdir(parents=True, exist_ok=False)
    ex.write(directory / 'candidate.json', candidate)
    return candidate_id


def code_candidate(workdir, identity, report_id, candidate_id):
    _, bundle, result = report(workdir, identity, report_id)
    if not ex.ID_RE.fullmatch(candidate_id):
        raise ValueError('代码候选编号无效')
    candidate = ex.read(root(workdir, identity) / 'reports' / report_id / 'code_candidates' / candidate_id / 'candidate.json')
    digest = ex.object_digest({k: v for k, v in candidate.items() if k != 'candidate_digest'})
    if (candidate['input_digest'] != ex.object_digest(bundle) or candidate['report_digest'] != result['result_digest']
            or digest != candidate['candidate_digest']):
        raise ValueError('代码候选摘要不一致')
    return candidate


def source_files(workdir, identity):
    record, bundle = read(workdir, identity)
    from .strategy_imports import SourceFile
    return [SourceFile(info['name'], (root(workdir, identity) / item['stored']).read_bytes())
            for info, item in zip(bundle['sources'], record['manifest'])]


def history(workdir, *, latest_only=False):
    records = []
    for path in (workdir / "runs/strategy_audits").glob("*/record.json"):
        try:
            record, _ = read(workdir, path.parent.name)
            records.append(record)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    records = sorted(records, key=lambda r: (r.get('version', 1), r['created_at'], r['id']), reverse=True)
    if latest_only:
        latest = {}
        for record in records:
            latest.setdefault(record.get('strategy_id', record['id']), record)
        records = list(latest.values())
    return sorted(records, key=lambda r: r['created_at'], reverse=True)
