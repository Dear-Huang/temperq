"""Portable policy rules, excluding account data and performance claims."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from . import experiments as ex,policy


def validate(value):
    if not isinstance(value,dict) or set(value)!={'schema','preset','defaults_version','policy','signal_requirement'}:
        raise ValueError('模板只接受政策字段，不接受账户、成绩或可执行代码')
    if value['schema']!='policy_template_v1' or value['preset'] not in policy.PRESETS:
        raise ValueError('不支持的政策模板版本或预设')
    if value['signal_requirement']!='cn_a_next_open_5d':raise ValueError('模板预测期限或市场不兼容')
    if not isinstance(value['defaults_version'],str) or len(value['defaults_version'])>80:raise ValueError('默认值版本无效')
    resolved=policy.Policy.model_validate(value['policy'])
    return {**value,'policy':resolved.model_dump(mode='json')}


def export(spec):
    r=spec['resolved']
    return validate({'schema':'policy_template_v1','preset':r['preset'],
        'defaults_version':r['defaults_version'],'policy':r['policy'],
        'signal_requirement':'cn_a_next_open_5d'})


def install(workdir,raw):
    if len(raw)>64000:raise ValueError('政策模板不能超过 64 KB')
    value=validate(json.loads(raw))
    sid=uuid.uuid4().hex
    root=Path(workdir)/'policy_templates'/sid
    root.mkdir(parents=True)
    ex.write(root/'template.json',value)
    return sid


def read(workdir,sid):
    if not ex.ID_RE.fullmatch(sid):raise ValueError('模板编号无效')
    value=validate(ex.read(Path(workdir)/'policy_templates'/sid/'template.json'))
    return value,{'template_hash':ex.object_digest(value),'defaults_version':value['defaults_version'],
        'scope':'rules_only_not_a_performance_claim'}
