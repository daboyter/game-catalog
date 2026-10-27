#!/usr/bin/env python3
"""
Read-only SQLite MCP server for the game catalog.

Exposes four tools to Claude:
  query_library   — run a read-only SELECT; returns rows as JSON
  search_games    — partial title search with store/compat filters (+ the user's status/rating)
  describe_schema — show table names and columns
  my_games        — the user's own play status (in progress / complete) and
                    1–10 ratings, with playtime, plus recently played games
                    they haven't marked

The status/ratings live in ~/.local/share/game-catalog/user/my-games.json
(my_games.py; edited from the portal), not in the database. This server
only reads them.

Written against the mcp 2.x SDK (MCPServer, formerly FastMCP). The 1.x
low-level Server decorators (@app.list_tools / @app.call_tool) no longer exist.

Register with Claude Code (user scope, stored in ~/.claude.json — NOT settings.json):
  claude mcp add --scope user game-library -- ssh <your-host> \
      '~/game-catalog-venv/bin/python3' '~/game-catalog/mcp_server.py'
(or drop the "ssh <your-host>" part when Claude Code runs on the same machine)
"""

import datetime as dt, json, pathlib, sqlite3
from mcp.server import MCPServer

import my_games as my_store         # same folder: the user's status/ratings (read-only here);
                                    # not "my_games": that's the tool's name below

DB_PATH = pathlib.Path.home() / ".local/share/game-catalog/library.db"

mcp = MCPServer("game-library")


def _open_db():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Library DB not found: {DB_PATH}")
    # mode=ro: the OS-level open is read-only, so no query can write
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@mcp.tool()
def describe_schema() -> str:
    """Return the schema of the game library database (table names and columns)."""
    conn = _open_db()
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        schema = {}
        for t in tables:
            tname = t["name"]
            cols  = conn.execute(f"PRAGMA table_info({tname})").fetchall()
            schema[tname] = [{"name": c["name"], "type": c["type"]} for c in cols]
    finally:
        conn.close()
    return json.dumps(schema, indent=2)


@mcp.tool()
def query_library(sql: str) -> str:
    """Run a read-only SELECT query against the game library database.

    Tables: games, ownership, game_tags, playtime, compat_notes, esde_entries.
    games has rating (IGDB 0–100), rating_count, and time to beat in hours:
    ttb_hastily_hours, ttb_normally_hours, ttb_completely_hours.
    game_tags(game_id, kind, value) — kind is genre, theme, mode, perspective,
    tag (curated playstyle: Roguelike, Metroidvania, Story rich, …), series,
    developer, publisher, keyword (raw, noisy). Filter e.g.
    EXISTS (SELECT 1 FROM game_tags t WHERE t.game_id = g.id AND t.kind = 'mode' AND t.value = 'Co-operative').
    ownership.store values: steam, epic, gog, amazon, gamepass, lutris, and battlenet,
    ea, ubisoft (Battle.net / EA App / Ubisoft Connect libraries; they play on bazzite via Lutris).
    ownership.kind values: purchased, free_claim, subscription, library (in a launcher
    account library), launcher (not a game).
    compat_notes.status values: works, partial, broken, untested.
    compat_notes.device values: odin2, bazzite, mac, steamdeck.
    The user's own play status and ratings are NOT in these tables: use the
    my_games tool (search_games also includes them).
    Returns up to 200 rows as JSON.
    """
    sql = sql.strip()
    if not sql.upper().startswith(("SELECT", "WITH")):
        return "ERROR: only SELECT queries are allowed"
    conn = _open_db()
    try:
        rows = conn.execute(sql).fetchmany(200)
        return json.dumps([dict(r) for r in rows], default=str)
    except sqlite3.Error as e:
        return f"ERROR: {e}"
    finally:
        conn.close()


@mcp.tool()
def search_games(q: str = "", store: str = "", compat: str = "", limit: int = 20) -> str:
    """Search owned games by title (partial match) with optional filters.

    store:  one of steam|epic|gog|amazon|gamepass|lutris|battlenet|ea|ubisoft
    compat: Odin 2 compat status, one of works|partial|broken|untested
    limit:  max results (default 20, max 100)
    Returns title, stores, year, genres, compat status, cover_url, short description,
    and the user's my_status ("in_progress" / "complete" / null) and my_rating (1–10 / null).
    """
    limit = max(1, min(int(limit), 100))

    where = ["o.kind != 'launcher'"]
    args  = []
    if q:
        where.append("LOWER(g.canonical_title) LIKE ?")
        args.append(f"%{q.lower()}%")
    if store:
        where.append("o.store = ?")
        args.append(store)
    where_sql = " AND ".join(where)

    compat_filter = ""
    if compat:
        rank = {"works": 0, "partial": 1, "broken": 2, "untested": 3}.get(compat, 3)
        compat_filter = f"HAVING compat_rank = {rank}"

    sql = f"""
        SELECT
            g.id, g.canonical_title, g.first_release_year, g.cover_url,
            g.description, g.igdb_id,
            GROUP_CONCAT(DISTINCT o.store)   AS stores,
            GROUP_CONCAT(DISTINCT o.store || ':' || o.store_id) AS refs,
            g.genres_json,
            MAX(CASE WHEN cn.status='works'   THEN 0
                     WHEN cn.status='partial' THEN 1
                     WHEN cn.status='broken'  THEN 2
                     ELSE 3 END)             AS compat_rank
        FROM games g
        JOIN  ownership    o  ON o.game_id = g.id
        LEFT JOIN compat_notes cn ON cn.game_id = g.id AND cn.device = 'odin2'
        WHERE {where_sql}
        GROUP BY g.id
        {compat_filter}
        ORDER BY g.canonical_title
        LIMIT {limit}
    """
    conn = _open_db()
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        return f"ERROR: {e}"
    finally:
        conn.close()

    result_list = []
    for r in rows:
        rank = r["compat_rank"] if r["compat_rank"] is not None else 3
        mine = my_store.find(my_store.game_keys(r["igdb_id"], (r["refs"] or "").split(","))) or {}
        result_list.append({
            "id":           r["id"],
            "title":        r["canonical_title"],
            "year":         r["first_release_year"],
            "stores":       (r["stores"] or "").split(","),
            "genres":       json.loads(r["genres_json"]) if r["genres_json"] else [],
            "compat_odin2": ["works", "partial", "broken", "untested"][min(rank, 3)],
            "cover_url":    r["cover_url"],
            "description":  (r["description"] or "")[:300],
            "my_status":    mine.get("status"),
            "my_rating":    mine.get("rating"),
        })
    return json.dumps(result_list, default=str)


RECENT_DAYS, MIN_PLAY_MINUTES = 30, 10      # same as the portal's "Suggested" (portal/mine.py)


@mcp.tool()
def my_games(status: str = "", min_rating: int = 0, include_suggested: bool = True) -> str:
    """The user's own play status and ratings, for recommendations.

    status:     "in_progress" or "complete" to filter; empty = all marked/rated
    min_rating: only entries the user rated at least this (1–10); 0 = no filter
    include_suggested: also list games/ROMs played in the last 30 days (10+ minutes)
                       that the user hasn't marked yet

    Each entry: kind (game = store game, rom = ES-DE ROM), title, my_status
    ("in_progress" / "complete" / null), my_rating (1–10 / null), updated,
    igdb_id, and from the library: games.id (game_id) or esde_entries system,
    plus playtime (store minutes and last played; ROMs: ES-DE play count,
    seconds and last played).
    "In progress" games are often "forever" games the user keeps returning to
    (Minecraft, No Man's Sky): don't treat them as unfinished business.
    Completed games shouldn't be recommended again.
    """
    entries = my_store.entries()
    conn = _open_db()
    try:
        out = []
        for key, e in sorted(entries.items(), key=lambda kv: kv[1].get("title", "").lower()):
            if status and e.get("status") != status:
                continue
            if min_rating and (e.get("rating") or 0) < min_rating:
                continue
            item = {"kind": e.get("kind"), "title": e.get("title"), "my_status": e.get("status"),
                    "my_rating": e.get("rating"), "updated": e.get("updated"), "igdb_id": e.get("igdb_id")}
            if e.get("kind") == "game":
                row = None
                if e.get("igdb_id"):
                    row = conn.execute("SELECT id FROM games WHERE igdb_id = ?", (e["igdb_id"],)).fetchone()
                for ref in e.get("refs", []) if not row else []:
                    store, _, sid = ref.partition(":")
                    row = conn.execute("SELECT game_id AS id FROM ownership WHERE store = ? AND store_id = ?",
                                       (store, sid)).fetchone()
                    if row:
                        break
                if row:
                    p = conn.execute("SELECT SUM(minutes_total) m, MAX(last_played_at) l FROM playtime "
                                     "WHERE game_id = ?", (row["id"],)).fetchone()
                    item.update(game_id=row["id"], playtime_minutes=p["m"], last_played=p["l"])
                else:
                    item["note"] = "not in the library right now"
            else:
                item["system"] = e.get("system")
                w, args = ("igdb_id = ?", e["igdb_id"]) if e.get("igdb_id") else ("rom_path = ?", e.get("path"))
                p = conn.execute(f"SELECT SUM(playcount) c, SUM(playtime_secs) s, MAX(lastplayed) l "
                                 f"FROM esde_entries WHERE system = ? AND {w}", (e.get("system"), args)).fetchone()
                item.update(play_count=p["c"], playtime_seconds=p["s"], last_played=p["l"])
            out.append(item)

        suggested = []
        if include_suggested:
            since = (dt.date.today() - dt.timedelta(days=RECENT_DAYS)).isoformat()
            for r in conn.execute("""
                    SELECT g.id, g.canonical_title, g.igdb_id, SUM(p.minutes_total) m, MAX(p.last_played_at) l,
                           (SELECT GROUP_CONCAT(o.store || ':' || o.store_id) FROM ownership o
                             WHERE o.game_id = g.id AND o.kind != 'launcher') refs
                    FROM playtime p JOIN games g ON g.id = p.game_id
                    GROUP BY g.id HAVING l >= ? AND m >= ? AND refs IS NOT NULL ORDER BY l DESC""",
                    (since, MIN_PLAY_MINUTES)):
                if not (my_store.find(my_store.game_keys(r["igdb_id"], r["refs"].split(","))) or {}).get("status"):
                    suggested.append({"kind": "game", "title": r["canonical_title"], "game_id": r["id"],
                                      "playtime_minutes": r["m"], "last_played": r["l"]})
            for r in conn.execute("""
                    SELECT system, igdb_id, MIN(es_name) t, GROUP_CONCAT(rom_path, char(31)) paths,
                           SUM(playtime_secs) s, MAX(lastplayed) l
                    FROM esde_entries WHERE lastplayed >= ?
                    GROUP BY system, COALESCE(igdb_id, es_name) HAVING s >= ? ORDER BY l DESC""",
                    (since.replace("-", ""), MIN_PLAY_MINUTES * 60)):
                keys = my_store.rom_keys(r["system"], r["igdb_id"], r["paths"].split("\x1f"))
                if not (my_store.find(keys) or {}).get("status"):
                    suggested.append({"kind": "rom", "title": r["t"], "system": r["system"],
                                      "playtime_seconds": r["s"], "last_played": r["l"]})
    finally:
        conn.close()
    result = {"my_games": out}
    if include_suggested:
        result["played_recently_not_marked"] = suggested
    return json.dumps(result, default=str, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run("stdio")
