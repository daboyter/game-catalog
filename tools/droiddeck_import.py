#!/usr/bin/env python3
"""
Load DroidDeck availability into the droiddeck_status table (runs in refresh.sh).

Source of truth: tools/droiddeck-scan/droiddeck-status.jsonl in the repo, one
line per Game Pass product as checked on the Odin 2 by the DroidDeck scanner
(installable / findable / missing / unsure, with the offer text and date).

Game Pass titles that have never been scanned (added to Game Pass since the
last scan) get a PREDICTED status from the Xodus package check on bazzite
(cache/xodus_resolve.jsonl), see predict(). Predictions are stored with
source='predicted' and the portal labels them as such; the next scan on the
Odin replaces them with checked results.

Usage:
    ~/game-catalog-venv/bin/python3 tools/droiddeck_import.py
"""

import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _db import open_db

HERE      = pathlib.Path(__file__).resolve().parent
SCANNED   = HERE / "droiddeck-scan/droiddeck-status.jsonl"
XODUS     = pathlib.Path.home() / ".local/share/game-catalog/cache/xodus_resolve.jsonl"


def xodus_category(x):
    """Same buckets as the scanner comparison: own / other / unsupported / none / error."""
    if not x or x.get("exit") != 0:
        return "error"
    if not x.get("desktop"):
        return "none"
    if not any(d.get("supported") for d in x["desktop"]):
        return "unsupported"
    return "own" if x.get("same_product") else "other"


# Measured against the full DroidDeck scan of 2026-10-23 (see README in
# droiddeck-scan/): which Xodus bucket predicts which DroidDeck result.
PREDICT = {
    "own":         "installable",
    "other":       "unsure",       # bundles/launchers usually work, Ubisoft stubs don't
    "unsupported": "missing",
    "none":        "missing",
    "error":       "missing",
}


def predict(x):
    return PREDICT[xodus_category(x)]


def main():
    scanned = {}
    if SCANNED.exists():
        for line in SCANNED.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                scanned[r["store_id"]] = r
    xodus = {}
    if XODUS.exists():
        xodus = {r["store_id"]: r for r in map(json.loads, XODUS.read_text().splitlines()) if r}

    with open_db() as db:
        rows = db.execute("SELECT store_id FROM ownership WHERE store = 'gamepass' "
                          "AND platforms IS NOT NULL").fetchall()
        db.execute("DELETE FROM droiddeck_status")
        n_scanned = n_pred = 0
        for (store_id,) in rows:
            x = xodus.get(store_id)
            s = scanned.get(store_id)
            if s:
                status, source, offer, checked = s["status"], s.get("source", "search"), s.get("offer"), s.get("checked_at")
                n_scanned += 1
            else:
                status, source, offer, checked = predict(x), "predicted", None, None
                n_pred += 1
            db.execute("""
                INSERT INTO droiddeck_status (store_id, status, source, offer, checked_at, xodus)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (store_id, status, source, offer, checked, xodus_category(x) if x else None))
        db.commit()
        counts = dict(db.execute("SELECT status, COUNT(*) FROM droiddeck_status GROUP BY status").fetchall())
    print(f"[droiddeck] {n_scanned} checked on the Odin 2, {n_pred} predicted from Xodus: {counts}")


if __name__ == "__main__":
    main()
