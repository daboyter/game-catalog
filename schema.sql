-- Game catalog schema
-- Database: ~/.local/share/game-catalog/library.db
-- Never committed to the repo; recreate with: python3 -c "from _db import open_db; open_db().__enter__()"
-- or: rm library.db && bash refresh.sh

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per unique real-world game.
-- igdb_id NULL means not yet matched to IGDB (import ran but IGDB lookup pending/failed).
CREATE TABLE IF NOT EXISTS games (
    id                  INTEGER PRIMARY KEY,
    igdb_id             INTEGER UNIQUE,             -- NULL until matched
    normalized_title    TEXT    NOT NULL UNIQUE,    -- lowercase, stripped, for matching
    canonical_title     TEXT    NOT NULL,           -- best display title (IGDB > store)
    first_release_year  INTEGER,
    cover_url           TEXT,
    genres_json         TEXT,                       -- JSON array of genre strings
    description         TEXT,
    igdb_matched_at     TEXT,                       -- ISO 8601; NULL = not yet tried
    -- Filled by enrich_details.py from IGDB (NULL for unmatched games)
    rating              REAL,                       -- IGDB total_rating, 0–100
    rating_count        INTEGER,                    -- IGDB total_rating_count
    ttb_hastily_hours   REAL,                       -- time to beat: main story, rushed
    ttb_normally_hours  REAL,                       -- time to beat: main + some extras
    ttb_completely_hours REAL,                      -- time to beat: 100%
    details_fetched_at  TEXT,                       -- ISO 8601
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS games_normalized_title ON games (normalized_title);
CREATE INDEX IF NOT EXISTS games_igdb_id          ON games (igdb_id);

-- Browsable labels, one row per game × kind × value. Drives the portal's
-- filter sidebar. REBUILT from scratch by enrich_details.py on every refresh.
-- kind:  genre       — normalized (IGDB + Game Pass names mapped to one list)
--        theme       — IGDB themes (Science fiction, Horror, Open world, …)
--        mode        — IGDB game modes (Single player, Co-operative, Split screen, …)
--        perspective — IGDB player perspectives (First person, Side view, …)
--        series      — IGDB franchises + collections
--        developer, publisher — IGDB involved companies
--        keyword     — raw IGDB keywords (noisy, lowercased)
--        tag         — curated playstyle tags grouped from keywords
--                      (enrich_details.py TAG_GROUPS: Roguelike, Metroidvania, …)
CREATE TABLE IF NOT EXISTS game_tags (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    kind    TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    PRIMARY KEY (game_id, kind, value)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS game_tags_kind_value ON game_tags (kind, value);

-- One row per game × store (ownership/claim record).
-- store: steam | epic | gog | amazon | gamepass | lutris |
--        battlenet | ea | ubisoft (account libraries synced by Lutris's launchers)
-- store_id: Steam appid (integer string), GOG product id (integer string),
--           Epic app_name (hex UUID), Amazon app_name (UUID),
--           Xbox storeId, Lutris slug, Battle.net product id (s1, wow, fenris),
--           EA contentId, Ubisoft spaceId
--
-- kind: purchased    — you paid for this title
--       free_claim   — claimed free (Epic weekly, Prime Gaming); persists after subscription ends
--       subscription — access requires active subscription (Game Pass); may disappear
--       library      — in a Battle.net / EA / Ubisoft account library (bought, or linked
--                      from Ubisoft+ / Game Pass: Lutris can't tell)
--       launcher     — not a game: a helper app (EA App, Battle.net, etc.), or an
--                      engine / tool / soundtrack listed in not-games.tsv (not_games.py)
CREATE TABLE IF NOT EXISTS ownership (
    id              INTEGER PRIMARY KEY,
    game_id         INTEGER NOT NULL REFERENCES games(id),
    store           TEXT    NOT NULL,
    store_id        TEXT    NOT NULL,
    store_title     TEXT    NOT NULL,
    kind            TEXT    NOT NULL DEFAULT 'purchased',  -- see above
    is_installed    INTEGER NOT NULL DEFAULT 0,            -- 1/0
    claimed_at      TEXT,                                  -- ISO 8601 if known
    art_cover_url   TEXT,
    art_square_url  TEXT,
    release_year    INTEGER,                               -- store's release year; filled by store_years.py
    platforms       TEXT,                                  -- gamepass only: "pc,cloud,console" (see importers/gamepass.py)
    raw_json        TEXT,                                  -- full raw source object (for debugging)
    imported_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (store, store_id)
);

CREATE INDEX IF NOT EXISTS ownership_game_id ON ownership (game_id);
CREATE INDEX IF NOT EXISTS ownership_store   ON ownership (store);
CREATE INDEX IF NOT EXISTS ownership_kind    ON ownership (kind);

-- Most-recent playtime per game × source.
-- source: steam | heroic | lutris | esde
CREATE TABLE IF NOT EXISTS playtime (
    id              INTEGER PRIMARY KEY,
    game_id         INTEGER NOT NULL REFERENCES games(id),
    source          TEXT    NOT NULL,
    store_id        TEXT,                        -- back-reference for debugging
    minutes_total   INTEGER NOT NULL DEFAULT 0,
    last_played_at  TEXT,                        -- ISO 8601 or NULL
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (game_id, source)
);

CREATE INDEX IF NOT EXISTS playtime_game_id ON playtime (game_id);

-- Manual compatibility notes, one row per game × device.
-- THIS TABLE IS REBUILT FROM compat-notes.tsv ON EVERY REFRESH.
-- The TSV is the source of truth; this table is a queryable copy.
-- ONLY load-compat-notes.py writes here. All importers must ignore this table.
--
-- device: odin2 | bazzite | (future: mac, steamdeck, ...)
-- status: works | partial | broken | untested
-- proton_version: e.g. "GE-Proton9-20" or "Proton-10.0-4" (the compat layer)
-- app_version:    game build/patch version if relevant, e.g. "1.3.2"
CREATE TABLE IF NOT EXISTS compat_notes (
    id              INTEGER PRIMARY KEY,
    game_id         INTEGER NOT NULL REFERENCES games(id),
    device          TEXT    NOT NULL DEFAULT 'odin2',
    status          TEXT    NOT NULL DEFAULT 'untested',
    notes           TEXT,
    proton_version  TEXT,
    app_version     TEXT,
    tested_at       TEXT,                        -- ISO 8601 date
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (game_id, device)
);

-- ES-DE ROM entries (importers/esde.py). Scraped metadata comes from the
-- gamelists; igdb_id is filled by match_roms.py (platform-aware exact-title match).
-- game_id_fk links a ROM to the store game that is the same IGDB game
-- (_db.link_esde_to_games), or, for ROMs/games without an IGDB match, has the
-- same normalized title. Most ROMs have no store game; that is expected.
CREATE TABLE IF NOT EXISTS esde_entries (
    id              INTEGER PRIMARY KEY,
    game_id_fk      INTEGER REFERENCES games(id),
    igdb_id         INTEGER,                     -- NULL = no exact IGDB match (yet)
    igdb_matched_at TEXT,                        -- ISO 8601; NULL = not tried yet
    system          TEXT    NOT NULL,            -- snesna, gb, psx, etc.
    rom_path        TEXT    NOT NULL,            -- path relative to ~/ROMs/<system>/
    es_name         TEXT    NOT NULL,            -- scraped display name
    genre           TEXT,
    developer       TEXT,
    publisher       TEXT,
    players         TEXT,
    rating          REAL,
    release_date    TEXT,
    description     TEXT,
    favorite        INTEGER NOT NULL DEFAULT 0,
    playcount       INTEGER NOT NULL DEFAULT 0,
    playtime_secs   INTEGER NOT NULL DEFAULT 0,
    lastplayed      TEXT,                        -- ISO 8601 or NULL
    imported_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (system, rom_path)
);

CREATE INDEX IF NOT EXISTS esde_game_id_fk ON esde_entries (game_id_fk);
CREATE INDEX IF NOT EXISTS esde_system     ON esde_entries (system);

-- Browsable labels for IGDB-matched ROMs, same kinds and normalization as
-- game_tags (genre, theme, mode, perspective, tag, keyword, series,
-- developer, publisher). REBUILT by match_roms.py on every run.
CREATE TABLE IF NOT EXISTS rom_tags (
    rom_id  INTEGER NOT NULL REFERENCES esde_entries(id) ON DELETE CASCADE,
    kind    TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    PRIMARY KEY (rom_id, kind, value)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS rom_tags_kind_value ON rom_tags (kind, value);

-- IGDB relations of the ROMs' games (match_roms.py, rebuilt every run), so a
-- ROM can find its remake / remaster / port among the store games, which are
-- separate IGDB games. relation: related_igdb_id is a remake | remaster | port
-- of igdb_id, or "original" (igdb_id is an edition of related_igdb_id).
CREATE TABLE IF NOT EXISTS igdb_relations (
    igdb_id         INTEGER NOT NULL,
    related_igdb_id INTEGER NOT NULL,
    relation        TEXT    NOT NULL,
    PRIMARY KEY (igdb_id, related_igdb_id, relation)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS igdb_relations_related ON igdb_relations (related_igdb_id);

-- Merged duplicate titles. When two store titles turn out to be one game
-- (both resolve to the same IGDB game — e.g. Game Pass "Fallout" and Epic
-- "Fallout: A Post Nuclear Role Playing Game"), merge.py folds one games row
-- into the other (_db.merge_games) and records the folded row's normalized
-- title here, so importers keep resolving it to the surviving game.
-- A cache of merge decisions, not a source of truth: a rebuilt DB starts
-- empty and merge.py re-merges the same way. To force a merge, pin both
-- store entries to the same igdb_id in igdb-pins.tsv.
CREATE TABLE IF NOT EXISTS game_aliases (
    normalized_title TEXT PRIMARY KEY,
    game_id          INTEGER NOT NULL REFERENCES games(id)
);

-- DroidDeck (Odin 2) availability per Game Pass product. REBUILT on every
-- refresh by tools/droiddeck_import.py from the scan results committed in
-- tools/droiddeck-scan/droiddeck-status.jsonl, plus predictions from the
-- Xodus package check for titles not scanned yet.
-- status: installable | findable (in DroidDeck, no Install) | missing | unsure
-- source: search | library (checked on the Odin 2) | predicted (from Xodus)
CREATE TABLE IF NOT EXISTS droiddeck_status (
    store_id    TEXT PRIMARY KEY,           -- ownership.store_id of the gamepass row
    status      TEXT NOT NULL,
    source      TEXT NOT NULL,
    offer       TEXT,                       -- e.g. "AVAILABLE WITH XBOX GAME PASS PREMIUM • 7.5 GB"
    checked_at  TEXT,                       -- date of the Odin 2 check (NULL if predicted)
    xodus       TEXT                        -- Xodus package bucket: own/other/unsupported/none/error
);

-- One row per refresh step per run (written by runlog.py from refresh.sh, and
-- by scans that run elsewhere, e.g. droiddeck from the Mac). The portal flags
-- a job whose latest run failed or is stuck, or that hasn't succeeded lately.
-- job: refresh (the whole run), epic-sync, import-<store>, xodus-resolve,
--      store-years, igdb-match, igdb-details, igdb-roms, compat-notes, droiddeck
CREATE TABLE IF NOT EXISTS scan_runs (
    id          INTEGER PRIMARY KEY,
    run         TEXT NOT NULL,              -- run stamp, e.g. 20261023-101500
    job         TEXT NOT NULL,
    trigger     TEXT,                       -- timer | manual | portal | mac
    status      TEXT NOT NULL,              -- running | ok | failed
    started_at  TEXT NOT NULL,              -- ISO 8601 UTC
    finished_at TEXT,
    log_path    TEXT,
    message     TEXT,                       -- failure reason (last log line)
    UNIQUE (run, job)
);

CREATE INDEX IF NOT EXISTS scan_runs_job ON scan_runs (job, started_at);

-- Replaced by game_aliases + igdb-pins.tsv; never had a loader.
DROP TABLE IF EXISTS title_overrides;
