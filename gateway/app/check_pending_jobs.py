"""Standalone check used by stop.ps1 to warn about unfinished jobs before
shutdown. Deliberately plain sqlite3 (not job_store's async wrappers) since
this runs as a one-off script, not inside the running event loop."""

import sqlite3
from pathlib import Path


def main() -> None:
    db_path = Path("/data/jobs.db")
    if not db_path.exists():
        return  # no jobs have ever been submitted

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM jobs WHERE status IN ('queued', 'processing') GROUP BY status"
    ).fetchall()
    conn.close()

    counts = dict(rows)
    if counts.get("processing"):
        print(f"  {counts['processing']} job(s) actively processing - will be marked FAILED (cannot resume)")
    if counts.get("queued"):
        print(f"  {counts['queued']} job(s) queued - preserved, will resume on next start.ps1")


if __name__ == "__main__":
    main()
