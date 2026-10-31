#!/usr/bin/env python3
"""
Record a scan step in the scan_runs table (called by refresh.sh for every step).

    runlog.py JOB STATUS STARTED LOG [MESSAGE] --run STAMP --trigger TRIGGER

STATUS is running / ok / failed. A "running" row is updated in place when the
same job + run finishes, so a step that dies mid-way (killed, power loss)
stays visible as "running" and the portal flags it as stuck.

Also used for scans that run elsewhere, e.g. the DroidDeck scan on the Mac:
    ssh ebonhawk.local '~/game-catalog-venv/bin/python3 ~/game-catalog/runlog.py droiddeck ok ...'
"""

import argparse
from _db import open_db

ap = argparse.ArgumentParser()
ap.add_argument("job")
ap.add_argument("status", choices=["running", "ok", "failed"])
ap.add_argument("started")
ap.add_argument("log")
ap.add_argument("message", nargs="?", default=None)
ap.add_argument("--run", required=True)
ap.add_argument("--trigger", default="manual")
a = ap.parse_args()

with open_db() as db:
    db.execute("""
        INSERT INTO scan_runs (run, job, trigger, status, started_at, finished_at, log_path, message)
        VALUES (?, ?, ?, ?, ?, CASE WHEN ? = 'running' THEN NULL ELSE strftime('%Y-%m-%dT%H:%M:%SZ','now') END, ?, ?)
        ON CONFLICT (run, job) DO UPDATE SET
            status      = excluded.status,
            finished_at = excluded.finished_at,
            message     = excluded.message
    """, (a.run, a.job, a.trigger, a.status, a.started, a.status, a.log, a.message))
    db.commit()
