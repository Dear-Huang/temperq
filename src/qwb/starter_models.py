"""Small, data-free model distributions with a fixed public-data recipe."""
from __future__ import annotations

from pathlib import Path
import shutil
import uuid

from . import experiments as ex

BUNDLED=Path(__file__).parent/'bundled'


def catalog():
    return {p.parent.name:ex.read(p) for p in sorted(BUNDLED.glob('*/distribution.json'))}


def verify(root):
    root=Path(root)
    card=ex.read(root/'distribution.json')
    model_id=card.get('id')
    if model_id not in catalog() or ex.digest(root/'distribution.json')!=ex.digest(BUNDLED/model_id/'distribution.json'):
        raise ValueError('入门模型清单与随包版本不一致')
    for name,digest in card['files'].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()) or path.is_symlink() or ex.digest(path)!=digest:
            raise ValueError('入门模型文件不完整或被修改：'+name)
    return card


def install(workdir,model_id):
    if model_id not in catalog():raise ValueError('未知入门模型')
    source=BUNDLED/model_id;verify(source)
    destination=Path(workdir)/'runs'/('starter_'+model_id)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with ex.output_lock(destination):
        if destination.exists():verify(destination);return destination
        temporary=destination.with_name('.install-'+uuid.uuid4().hex)
        try:
            shutil.copytree(source,temporary);verify(temporary);temporary.rename(destination)
        finally:
            if temporary.exists():shutil.rmtree(temporary)
    return destination
