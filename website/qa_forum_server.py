"""Isolated database for browser workflow verification."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QWB_SITE_AUTOSTART'] = '0'
from share_site.app import create_app
import uvicorn

with tempfile.TemporaryDirectory(prefix='qwb-forum-qa-') as directory:
    uvicorn.run(create_app(Path(directory) / 'site.db', oauth={}, secret='browser-qa'),
                host='127.0.0.1', port=8801)
