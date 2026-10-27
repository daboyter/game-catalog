"""
Scan health and rescans — /scans page, the header warning, and rescan buttons.

Health comes from the scan_runs table that refresh.sh fills (via runlog.py)
for every step. A job "needs attention" when its latest run:
  - failed,
  - is still "running" long after it should have finished (killed mid-way), or
  - last succeeded longer ago than its STALE_DAYS (e.g. the weekly timer stopped).

Rescans run refresh.sh detached through `systemd-run --user`, so they keep going
after the request returns and survive a portal restart. refresh.sh holds a
lock, so a rescan can never overlap the weekly timer run.
"""

import datetime as dt, pathlib, re, subprocess
from flask import Blueprint, abort, redirect, render_template, request, url_for

LOG_ROOT    = pathlib.Path.home() / ".local/share/game-catalog/logs"
REFRESH_SH  = pathlib.Path(__file__).resolve().parent.parent / "refresh.sh"
RESCAN_UNIT = "game-catalog-rescan"
STUCK_AFTER = dt.timedelta(hours=2)
LOG_TAIL    = 40

# job → (label, days without a successful run before it's flagged; None = never)
JOBS = {
    "refresh":         ("Whole refresh",                     8),
    "epic-sync":       ("Epic library sync (legendary)",      8),
    "heroic-sync":     ("GOG / Prime library sync (Heroic)",  8),
    "import-steam":    ("Steam importer",                     8),
    "import-heroic":   ("Heroic importer (Epic, GOG, Prime)", 8),
    "import-xodus":    ("Xodus Nexus importer",               8),
    "import-esde":     ("ES-DE ROMs importer",                8),
    "import-lutris":   ("Lutris importer",                    8),
    "import-gamepass": ("Game Pass catalog",                  8),
    "not-games":       ("Hide non-games (not-games.tsv)",     8),
    "xodus-resolve":   ("Game Pass PC packages (Xodus)",      8),
    "droiddeck-import": ("DroidDeck status (results + predictions)", 8),
    "store-years":     ("Store release years",                8),
    "igdb-match":      ("IGDB matching",                      8),
    "igdb-details":    ("IGDB browsing metadata",             8),
    "igdb-roms":       ("IGDB matching for ES-DE ROMs",       8),
    "compat-notes":    ("Compat notes",                       8),
    "droiddeck":       ("DroidDeck availability (Odin 2, from the Mac)", None),
}

# rescan targets offered on the page: (value, label)
RESCANS = [
    ("full",     "Refresh everything"),
    ("steam",    "Steam"),
    ("heroic",   "Epic / GOG / Prime (Heroic)"),
    ("gamepass", "Game Pass"),
    ("xodus",    "Xodus Nexus"),
    ("esde",     "ES-DE ROMs"),
    ("lutris",   "Lutris"),
]


def _parse(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def _ago(when, now):
    if not when:
        return "never"
    s = (now - when).total_seconds()
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            return f"{int(s // n)} {unit} ago"
    return "just now"


def job_health(db):
    """One dict per job with its latest run, last success and a problem string (or None)."""
    now = dt.datetime.now(dt.timezone.utc)
    rows = db.execute("""
        SELECT r.* FROM scan_runs r
        JOIN (SELECT job, MAX(started_at) AS latest FROM scan_runs GROUP BY job) l
          ON l.job = r.job AND l.latest = r.started_at
    """).fetchall()
    last_ok = dict(db.execute(
        "SELECT job, MAX(finished_at) FROM scan_runs WHERE status = 'ok' GROUP BY job").fetchall())
    latest = {r["job"]: r for r in rows}
    out = []
    for job in list(JOBS) + sorted(set(latest) - set(JOBS)):
        label, stale_days = JOBS.get(job, (job, 8))
        r = latest.get(job)
        ok_at = _parse(last_ok.get(job))
        problem = None
        if r is None:
            if job not in ("droiddeck",) and job != "epic-sync":
                problem = "has never run"
        elif r["status"] == "failed":
            problem = r["message"] or "failed"
        elif r["status"] == "running" and now - _parse(r["started_at"]) > STUCK_AFTER:
            problem = "stuck: started over 2 h ago and never finished (killed?)"
        elif stale_days and ok_at and now - ok_at > dt.timedelta(days=stale_days):
            problem = f"no successful run in {(now - ok_at).days} days"
        started = _parse(r["started_at"]) if r else None
        finished = _parse(r["finished_at"]) if r else None
        out.append({
            "job": job, "label": label, "row": r, "problem": problem,
            "status": r["status"] if r else "never",
            "when": _ago(started, now), "trigger": r["trigger"] if r else None,
            "duration": (f"{int((finished - started).total_seconds())} s"
                         if started and finished else None),
            "last_ok": _ago(ok_at, now),
        })
    return out


def log_tail(path):
    """Last LOG_TAIL lines of a step log — only files under LOG_ROOT are read."""
    if not path:
        return None
    p = pathlib.Path(path).resolve()
    if not p.is_relative_to(LOG_ROOT.resolve()) or not p.is_file():
        return None
    lines = p.read_text(errors="replace").splitlines()
    return "\n".join(lines[-LOG_TAIL:])


def rescan_running():
    r = subprocess.run(["systemctl", "--user", "is-active", f"{RESCAN_UNIT}.service",
                        "game-catalog-refresh.service"], capture_output=True, text=True)
    return "active" in r.stdout.split() or "activating" in r.stdout.split()


def make_blueprint(get_db):
    bp = Blueprint("scans", __name__)

    @bp.app_context_processor
    def scan_warning():
        """Header flag on every page: how many jobs need attention."""
        try:
            problems = [j for j in job_health(get_db()) if j["problem"]]
        except Exception:                      # never let the banner break a page
            problems = []
        return {"scan_problems": problems}

    @bp.route("/scans")
    def scans():
        jobs = job_health(get_db())
        for j in jobs:
            j["log"] = log_tail(j["row"]["log_path"]) if j["row"] else None
        running = rescan_running()
        return render_template("scans.html", jobs=jobs, rescans=RESCANS, running=running,
                               started=request.args.get("started"))

    @bp.route("/scans/rescan", methods=["POST"])
    def rescan():
        target = request.form.get("target", "")
        if target not in dict(RESCANS):
            abort(400)
        if rescan_running():
            return redirect(url_for("scans.scans", started="busy"))
        args = ["bash", str(REFRESH_SH), "--trigger", "portal"]
        if target != "full":
            args += ["--only", target]
        subprocess.run(["systemd-run", "--user", "--collect", f"--unit={RESCAN_UNIT}",
                        "--description=Game catalog rescan (from the portal)", *args],
                       check=True, capture_output=True)
        return redirect(url_for("scans.scans", started=target))

    return bp
