"""Run the workbench in a new isolated directory without opening user accounts."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import uvicorn
from qwb import ui


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--port", type=int, default=8846)
    args = parser.parse_args()
    workdir = args.output.resolve()
    if workdir.exists():
        raise SystemExit("Use a new directory; existing workspaces are never modified by this QA server.")
    workdir.mkdir(parents=True)
    port = ui.free_port(args.port)
    print(f"http://127.0.0.1:{port}/", flush=True)
    uvicorn.run(ui.create_app(workdir, port=port), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
