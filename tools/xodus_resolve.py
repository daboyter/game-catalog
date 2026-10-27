#!/usr/bin/env python3
"""
Ask Xodus Nexus which Game Pass titles have an installable Windows PC package.

Runs `xodus-cli resolve-package --json <productId>` (from Xodus Nexus's own
backend, ~/.config/xodus-nexus/xodus-backends/current) for every gamepass
ownership row. That command reads public Store metadata only: no sign-in,
nothing downloaded. Account commands (library, profile, license, ...) are
deliberately never used here.

Output: one JSON line per product in OUT (default
~/.local/share/game-catalog/cache/xodus_resolve.jsonl), resumable:
  {"store_id", "title", "exit", "status", "error",
   "desktop": [{"productId", "title", "format", "supported"}, ...],
   "same_product": bool}    # the Windows package belongs to the title itself
                             # (False = delivered via another product: a launcher
                             #  or a publisher stub, e.g. Ubisoft games)

Usage:
    ~/game-catalog-venv/bin/python3 tools/xodus_resolve.py [--refresh]
"""

import argparse, json, pathlib, sqlite3, subprocess, sys, time

HOME = pathlib.Path.home()
DB = HOME / ".local/share/game-catalog/library.db"
OUT = HOME / ".local/share/game-catalog/cache/xodus_resolve.jsonl"
BACKENDS = HOME / ".config/xodus-nexus/xodus-backends/releases"
DELAY = 0.5   # seconds between Store lookups


def find_cli():
    clis = sorted(BACKENDS.glob("*/xodus-cli"))
    if not clis:
        sys.exit(f"xodus-cli not found under {BACKENDS} — is Xodus Nexus installed?")
    return clis[-1]


def resolve(cli, store_id):
    r = subprocess.run([str(cli), "resolve-package", "--json", store_id],
                       capture_output=True, text=True, timeout=90)
    rec = {"exit": r.returncode, "status": None, "error": None, "desktop": [], "same_product": None}
    if r.returncode != 0 or not r.stdout.strip():
        rec["error"] = (r.stderr.strip().splitlines() or ["?"])[-1][:200]
        return rec
    d = json.loads(r.stdout)
    rec["status"] = d.get("status")
    games = [c for c in d.get("choices", []) if c.get("kind") == "Game"
             and c.get("platform") == "Windows.Desktop"]
    rec["desktop"] = [{"productId": c["productId"], "title": c.get("title"),
                       "format": c.get("packageFormat"), "supported": c.get("supported")} for c in games]
    rec["same_product"] = any(c["productId"] == store_id for c in games) if games else None
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-resolve titles already in OUT")
    args = ap.parse_args()

    cli = find_cli()
    rows = sqlite3.connect(DB).execute(
        "SELECT store_id, store_title FROM ownership WHERE store = 'gamepass' AND platforms IS NOT NULL"
    ).fetchall()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if OUT.exists() and not args.refresh:
        done = {json.loads(l)["store_id"] for l in OUT.read_text().splitlines()}
    elif args.refresh:
        OUT.write_text("")
    todo = [r for r in rows if r[0] not in done]
    print(f"[xodus] {len(todo)} to resolve ({len(done)} cached) with {cli.parent.name}", flush=True)
    with OUT.open("a") as f:
        for i, (store_id, title) in enumerate(todo, 1):
            try:
                rec = resolve(cli, store_id)
            except subprocess.TimeoutExpired:
                rec = {"exit": None, "status": None, "error": "timeout", "desktop": [], "same_product": None}
            f.write(json.dumps({"store_id": store_id, "title": title, **rec}, ensure_ascii=False) + "\n")
            f.flush()
            if i % 50 == 0:
                print(f"  [xodus] {i}/{len(todo)}", flush=True)
            time.sleep(DELAY)
    print("[xodus] done", flush=True)


if __name__ == "__main__":
    main()
