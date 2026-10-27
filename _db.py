"""
Shared database helpers for all importers.

Importers (in importers/) do:
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from _db import open_db, normalize_title, upsert_game

Scripts at the game-catalog root (merge.py, load-compat-notes.py) do:
    from _db import open_db, normalize_title, upsert_game
"""

import pathlib, re, sqlite3, unicodedata
from contextlib import contextmanager

DB_PATH    = pathlib.Path.home() / ".local/share/game-catalog/library.db"
SCHEMA_SQL = pathlib.Path(__file__).parent / "schema.sql"

# Your own lists (hidden games, IGDB pins, compat notes, ...) live here, next
# to steam.env and igdb.env, so they stay out of the repo. A list that isn't
# there falls back to the repo's copy: an empty template, or for not-games.tsv
# the shared default.
CONFIG_DIR = pathlib.Path.home() / ".config/game-catalog"


def list_file(name: str) -> pathlib.Path:
    """~/.config/game-catalog/<name> if you have one, otherwise the repo copy."""
    mine = CONFIG_DIR / name
    return mine if mine.exists() else pathlib.Path(__file__).parent / name

# Columns added after the first databases were created. schema.sql has them
# too, but CREATE TABLE IF NOT EXISTS won't add columns to an existing table.
MIGRATION_COLUMNS = [
    ("ownership", "release_year",         "INTEGER"),
    ("ownership", "platforms",            "TEXT"),
    ("games",     "rating",               "REAL"),
    ("games",     "rating_count",         "INTEGER"),
    ("games",     "ttb_hastily_hours",    "REAL"),
    ("games",     "ttb_normally_hours",   "REAL"),
    ("games",     "ttb_completely_hours", "REAL"),
    ("games",     "details_fetched_at",   "TEXT"),
    ("esde_entries", "igdb_id",           "INTEGER"),
    ("esde_entries", "igdb_matched_at",   "TEXT"),
]


@contextmanager
def open_db():
    """Open (and initialize if new) the catalog database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout: wait up to 60s for another writer (e.g. a manual merge.py run)
    # instead of failing with "database is locked" after the 5s default.
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")

    # Create tables if they don't exist yet
    conn.executescript(SCHEMA_SQL.read_text())

    # Add any MIGRATION_COLUMNS an older database is missing
    for table, col, coltype in MIGRATION_COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.execute("CREATE INDEX IF NOT EXISTS esde_igdb_id ON esde_entries (igdb_id)")
    conn.commit()

    try:
        yield conn
    finally:
        conn.close()


def normalize_title(title: str) -> str:
    """
    Produce a stable, lowercase slug for cross-store matching.
    - Unicode NFKD, drop combining marks (accents)
    - Lowercase
    - Strip leading articles: the, a, an
    - Remove all non-alphanumeric characters
    - Collapse whitespace

    Examples:
        "Half-Life 2"           → "halflife2"
        "The Witcher 3: Wild Hunt" → "witcher3wildhunt"
        "Pokémon Red"           → "pokemonred"
    """
    if not title:
        return ""
    # NFKD decompose and drop combining marks
    s = unicodedata.normalize("NFKD", title)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    # Strip leading articles
    s = re.sub(r"^(the|an?)\s+", "", s)
    # Keep only alphanumeric
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def find_game_id(db: sqlite3.Connection, norm: str) -> int | None:
    """
    games.id for a normalized title, following game_aliases for titles whose
    row was merged into another game (see merge_games). None if unknown.
    """
    row = db.execute("SELECT id FROM games WHERE normalized_title = ?", (norm,)).fetchone()
    if not row:
        row = db.execute("SELECT game_id FROM game_aliases WHERE normalized_title = ?",
                         (norm,)).fetchone()
    return row[0] if row else None


def merge_games(db: sqlite3.Connection, src_id: int, dst_id: int) -> None:
    """
    Fold game src_id into dst_id: the same game listed under two store titles
    (e.g. Game Pass "Fallout" vs Epic "Fallout: A Post Nuclear Role Playing Game").

    Moves ownership, playtime, compat notes and ES-DE links to dst, records
    src's normalized title in game_aliases so importers keep resolving it to
    dst on later refreshes, then deletes src. Caller commits.

    Aliases are a cache of merge decisions, not a source of truth: after a DB
    rebuild the duplicate reappears and merge.py merges it again (it is
    detected the same way — both titles resolve to one IGDB game).
    """
    if src_id == dst_id:
        return
    src_norm = db.execute("SELECT normalized_title FROM games WHERE id = ?",
                          (src_id,)).fetchone()[0]

    db.execute("UPDATE ownership SET game_id = ? WHERE game_id = ?", (dst_id, src_id))
    db.execute("UPDATE esde_entries SET game_id_fk = ? WHERE game_id_fk = ?", (dst_id, src_id))

    # playtime / compat_notes are UNIQUE per (game, source|device): move rows
    # dst lacks; where both have one, keep dst's playtime but take the larger
    # totals and the later last-played date.
    for p in db.execute("SELECT * FROM playtime WHERE game_id = ?", (src_id,)).fetchall():
        db.execute("""
            INSERT INTO playtime (game_id, source, store_id, minutes_total, last_played_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (game_id, source) DO UPDATE SET
                minutes_total  = MAX(minutes_total, excluded.minutes_total),
                last_played_at = MAX(COALESCE(last_played_at, ''), COALESCE(excluded.last_played_at, ''))
        """, (dst_id, p["source"], p["store_id"], p["minutes_total"], p["last_played_at"]))
    db.execute("DELETE FROM playtime WHERE game_id = ?", (src_id,))
    db.execute("UPDATE OR IGNORE compat_notes SET game_id = ? WHERE game_id = ?", (dst_id, src_id))
    db.execute("DELETE FROM compat_notes WHERE game_id = ?", (src_id,))

    db.execute("UPDATE game_aliases SET game_id = ? WHERE game_id = ?", (dst_id, src_id))
    db.execute("INSERT OR REPLACE INTO game_aliases (normalized_title, game_id) VALUES (?, ?)",
               (src_norm, dst_id))
    db.execute("DELETE FROM game_tags WHERE game_id = ?", (src_id,))
    db.execute("DELETE FROM games WHERE id = ?", (src_id,))


def link_esde_to_games(db: sqlite3.Connection) -> int:
    """
    Point esde_entries.game_id_fk at the store game with the same IGDB id
    (match_roms.py fills esde_entries.igdb_id). The title link importers/esde.py
    makes is kept only where IGDB can't contradict it: a ROM and a game that are
    both IGDB-matched to DIFFERENT games are unlinked (SNES "Prince of Persia"
    1992 is not the 2008 store game). Returns the number of linked ROMs.
    Caller commits.
    """
    db.execute("""
        UPDATE esde_entries SET game_id_fk = (
            SELECT g.id FROM games g WHERE g.igdb_id = esde_entries.igdb_id)
        WHERE igdb_id IN (SELECT igdb_id FROM games WHERE igdb_id IS NOT NULL)
    """)
    db.execute("""
        UPDATE esde_entries SET game_id_fk = NULL
        WHERE igdb_id IS NOT NULL AND game_id_fk IN (
            SELECT id FROM games WHERE igdb_id IS NOT NULL AND igdb_id != esde_entries.igdb_id)
    """)
    return db.execute("SELECT COUNT(*) FROM esde_entries WHERE game_id_fk IS NOT NULL").fetchone()[0]


def upsert_game(db: sqlite3.Connection, title: str,
                genres_json: str = None) -> tuple[int, bool]:
    """
    Find or create a game row by normalized title (following game_aliases).

    Returns (game_id, created).
    - If a row with this normalized_title already exists, returns its id and False.
    - Otherwise inserts a new row and returns the new id and True.

    If genres_json is provided and the existing row has no genres, it is updated.
    """
    norm = normalize_title(title)
    if not norm:
        raise ValueError(f"normalize_title({title!r}) returned empty string")

    found = find_game_id(db, norm)
    existing = found and db.execute(
        "SELECT id, genres_json FROM games WHERE id = ?", (found,)
    ).fetchone()

    if existing:
        game_id = existing[0]
        # Back-fill genres if the existing row is missing them
        if genres_json and not existing[1]:
            db.execute(
                "UPDATE games SET genres_json = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                " WHERE id = ?",
                (genres_json, game_id)
            )
        return game_id, False

    cur = db.execute(
        "INSERT INTO games (normalized_title, canonical_title, genres_json) VALUES (?, ?, ?)",
        (norm, title, genres_json)
    )
    return cur.lastrowid, True


# ── hidden games (hidden-games.tsv) ───────────────────────────────────────────

HIDDEN_FILE = list_file("hidden-games.tsv")


def load_hidden() -> set[tuple[str, str]]:
    """Read hidden-games.tsv → {(store, store_id), ...}."""
    hidden = set()
    if not HIDDEN_FILE.exists():
        return hidden
    for line in HIDDEN_FILE.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cols = line.split("\t")
        if cols[0] == "store" or len(cols) < 2:     # header / malformed
            continue
        hidden.add((cols[0].strip(), cols[1].strip()))
    return hidden


def drop_hidden(db: sqlite3.Connection, hidden: set[tuple[str, str]] = None) -> int:
    """
    Delete the ownership rows listed in hidden-games.tsv, then every games row
    left with no ownership because of it (with its tags, playtime, compat
    notes and aliases; ES-DE links are cleared). A game still owned in another
    store keeps its row. Returns the number of games deleted. Caller commits.
    """
    if hidden is None:
        hidden = load_hidden()
    affected = set()
    for store, store_id in hidden:
        row = db.execute("SELECT game_id FROM ownership WHERE store = ? AND store_id = ?",
                         (store, store_id)).fetchone()
        if row:
            affected.add(row[0])
            db.execute("DELETE FROM ownership WHERE store = ? AND store_id = ?",
                       (store, store_id))
    dropped = 0
    for gid in affected:
        if db.execute("SELECT 1 FROM ownership WHERE game_id = ?", (gid,)).fetchone():
            continue
        for table in ("playtime", "compat_notes", "game_tags", "game_aliases"):
            db.execute(f"DELETE FROM {table} WHERE game_id = ?", (gid,))
        db.execute("UPDATE esde_entries SET game_id_fk = NULL WHERE game_id_fk = ?", (gid,))
        db.execute("DELETE FROM games WHERE id = ?", (gid,))
        dropped += 1
    return dropped
