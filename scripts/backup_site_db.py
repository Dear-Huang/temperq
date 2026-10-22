"""Back up the share site's SQLite database while the site is running.

    python -X utf8 scripts/backup_site_db.py [db_path] [backup_dir] [keep]

Uses SQLite's online backup API, so a consistent copy is taken even while the
site writes. Keeps the newest `keep` copies (default 14). Run it daily from cron
or Task Scheduler, and copy backup_dir off the server.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def backup(db: Path, out_dir: Path, keep: int = 14) -> Path:
    if not db.is_file():
        raise FileNotFoundError(db)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = out_dir / f"{db.stem}-{stamp}.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        ok = dst.execute("pragma integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed integrity check: {ok}")
    copies = sorted(out_dir.glob(f"{db.stem}-*.db"))
    for old in copies[:-keep]:
        old.unlink()
    return dest


if __name__ == "__main__":
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "runs" / "site.db"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "runs" / "site_backups"
    keep = int(sys.argv[3]) if len(sys.argv) > 3 else 14
    print(backup(db, out, keep))
