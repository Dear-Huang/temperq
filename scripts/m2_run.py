"""M2 runner, kept for the commands in earlier reports. Same as `qwb run`:

    python -X utf8 scripts/m2_run.py            -> examples/run_cn_rank_5d.yaml
    python -X utf8 scripts/m2_run.py --smoke    -> examples/run_smoke.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import qwb  # noqa: E402,F401  (import-order guard first)
from qwb import cli  # noqa: E402

if __name__ == "__main__":
    name = "run_smoke.yaml" if "--smoke" in sys.argv[1:] else "run_cn_rank_5d.yaml"
    sys.exit(cli.main(["run", str(ROOT / "examples" / name)]))
