"""Relocate the ZIP; start via BAT and train via detached worker without tools/network."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.parse
import urllib.request
import zipfile


def check(archive: Path, destination: Path):
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Use a new destination: {destination}")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            if not (destination / item.filename).resolve().is_relative_to(destination):
                raise ValueError("Archive path escapes destination")
        z.extractall(destination)
    app = destination / "TemperQ"
    manifest = json.loads((app / "MANIFEST.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files_sha256"].items():
        assert hashlib.sha256((app / name).read_bytes()).hexdigest() == digest, name
    # Applied only to this disposable extracted copy, in parent AND worker Python.
    blocker = app / "runtime/Lib/site-packages/sitecustomize.py"
    blocker.write_text('''import sys
def offline(event, args):
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise OSError("Portable acceptance: external network disabled")
sys.addaudithook(offline)
''', encoding="utf-8")
    env = dict(os.environ, PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
               PYTHONHOME="Z:/no-system-python", PYTHONPATH="Z:/no-source-checkout",
               PYTHONNOUSERSITE="1", PYTHONUTF8="1")
    cli = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "temperq.bat", "--help"],
                         cwd=app, env=env, capture_output=True, encoding="utf-8", timeout=30)
    assert cli.returncode == 0 and "temperq" in cli.stdout.lower(), cli.stderr
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(route):
        return opener.open(base + route, timeout=10).read().decode()
    def post(route, form):
        return opener.open(urllib.request.Request(base + route, data=urllib.parse.urlencode(form).encode()), timeout=10)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = time.monotonic()
    with (destination / "startup.log").open("wb") as log:
        process = subprocess.Popen([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "start.bat",
                                    "--no-browser", "--port", str(port)], cwd=app, env=env,
                                   stdout=log, stderr=log, creationflags=flags)
        try:
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError((destination / "startup.log").read_text(encoding="utf-8", errors="replace"))
                try:
                    page = get("/")
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                raise TimeoutError("Portable startup timed out")
            assert "TemperQ" in page and "一键下载免费数据（BaoStock）" in page
            assert len(opener.open(base + "/static/dm-sans.ttf").read()) > 1000
            token = re.search('name="token" value="([^"]+)"', page)[1]
            fields = dict(token=token, name="portable_acceptance", market="cn_a", horizon="5",
                          universe="all", capital="lt30w", model="lightgbm", dataset="__synthetic__",
                          num_boost_round="40", n_seeds="1")
            # Use the actual synthetic dataset value rendered by this version.
            fields["dataset"] = re.findall('name="dataset" value="([^"]+)"', page)[0]
            with post("/run", fields) as response:
                job_url = response.url
                body = response.read().decode()
            assert "/job/" in job_url, body[-2000:]
            job_id = job_url.rsplit("/", 1)[-1]
            job = app / "runs/jobs" / job_id
            for _ in range(240):
                status = json.loads((job / "status.json").read_text(encoding="utf-8"))
                if status["state"] not in ("queued", "running"):
                    break
                time.sleep(0.5)
            assert status["state"] == "succeeded", status
            report = get("/job/" + job_id)
            assert "一句话结论" in report and "完整报告" in report
            assert (app / "runs/portable_acceptance/final_model/meta.json").is_file()
            output = {"status": "PASS", "source_commit": manifest["source_commit"],
                      "relocated_to": str(app), "startup": "start.bat", "external_network": "blocked by Python audit hook",
                      "path": env["PATH"], "user_site": "disabled", "gpu": "not bundled",
                      "detached_training_job": job_id, "elapsed_seconds": round(time.monotonic() - started, 2),
                      "limit": "Same Windows host; fresh-machine CI and nondeveloper trials remain separate acceptance."}
            (destination / "acceptance.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(output, ensure_ascii=False, indent=2), flush=True)
        finally:
            # Only the process tree created above, never other user services.
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            process.wait(timeout=30)
            blocker.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    check(args.archive, args.destination)
