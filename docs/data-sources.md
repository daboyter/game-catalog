# Where each launcher keeps its library (Linux)

Notes from building the importers on Bazzite (Fedora Atomic, KDE) in October
2026. Paths are the defaults for each app; versions were current at the time
and formats may change. Each section lists where the data lives, what it
gives you, how fresh it is, and the gotchas the importers work around.

---

## Steam (native install)

### Where it lives
- Binary: `/usr/bin/steam` (a layered system package on Bazzite, not a Flatpak)
- Installed-game manifests: `~/.local/share/Steam/steamapps/*.acf`
- User profile: `~/.local/share/Steam/userdata/<your-account-id>/config/localconfig.vdf`
- App metadata cache: `~/.local/share/Steam/appcache/appinfo.vdf` (binary VDF)
- API credentials for this project: `~/.config/game-catalog/steam.env`

### What the local files give you
- **ACF manifests**: installed games only (plus Proton, the Steam Linux
  Runtime and Steamworks redistributables, which aren't games).
- **localconfig.vdf**: preferences, not per-app playtime. Steam keeps
  playtime server-side, so the Web API is the authoritative source.
- **appinfo.vdf**: store metadata for locally known apps, in binary VDF.
  Needs a dedicated parser; the importers don't use it.

### Steam Web API
`IPlayerService/GetOwnedGames` with `include_appinfo=true` returns the full
owned library, installed or not. That's what `importers/steam.py` uses.

| Field | Notes |
|---|---|
| `appid` | Unique Steam app ID |
| `name` | Title |
| `playtime_forever` | Total minutes played (all platforms) |
| `playtime_linux_forever` | Linux minutes |
| `playtime_deck_forever` | Steam Deck minutes |
| `rtime_last_played` | Unix timestamp; 0 = never played |
| `img_icon_url` | Icon hash. Full-size art: `https://cdn.cloudflare.steamstatic.com/steam/apps/<appid>/capsule_231x87.jpg` |
| `content_descriptorids` | Content rating flags |

### Gotchas
- No genres or tags in `GetOwnedGames`; IGDB fills those in (one call,
  better coverage than per-app store lookups).
- No cover art beyond a small icon: use the CDN pattern above or IGDB.
- If the API returns an empty library, check Steam → Privacy Settings →
  Game details.

---

## Heroic Games Launcher (Epic, GOG, Amazon Prime Gaming)

### Where it lives
- App: Flatpak `com.heroicgameslauncher.hgl` (system-wide on Bazzite)
- Cache files: `~/.var/app/com.heroicgameslauncher.hgl/config/heroic/store_cache/`
- Auth tokens: `.../config/heroic/gog_store/auth.json` and the `nile_config/`
  folder. Credentials: never read or copy these.

### Epic (Legendary backend)
- **Best source**: `legendaryConfig/legendary/metadata/*.json`, one file per
  game, current after a `legendary list-games` run (see headless sync below).
- **Fallback**: `store_cache/legendary_library.json`, only rewritten when
  Heroic's window opens.
- **Fields**: `title`, `app_name` (hex ID), `developer`, `is_installed`,
  `extra.about.description`, `extra.storeUrl`, `art_cover`, `art_square`.
- No genres; IGDB fills those in.

### GOG
- **File**: `store_cache/gog_library.json` (full owned library; the first
  entry is a redistributable, so filter it).
- **Fields**: `title`, `app_name` (numeric GOG product ID), `is_installed`,
  `art_cover`, `art_square`, sometimes `extra.genres`.

### Amazon Prime Gaming (Nile backend)
- **File**: `nile_config/nile/library.json` (or `store_cache/nile_library.json`).
- **Fields**: `title`, `app_name` (UUID), `description`, `developer`,
  `extra.genres`, `extra.releaseDate`, several art URLs.

### Freshness
All three are full owned/claimed libraries, not just installed games. Each
cache file's root `__timestamp` says when it was written. Heroic doesn't
refresh in the background, so titles claimed since Heroic last ran are
missing until it runs again.

### Headless sync
- **Epic** works headlessly: Heroic bundles the `legendary` binary under
  `/var/lib/flatpak/app/com.heroicgameslauncher.hgl/.../bin/x64/linux/legendary`.
  Run `legendary list-games -q` with
  `LEGENDARY_CONFIG_PATH=~/.var/app/com.heroicgameslauncher.hgl/config/heroic/legendaryConfig/legendary`
  (about 30 seconds). `refresh.sh` does this.
- **Amazon** can't sync headlessly: Nile's token is device-encrypted, and
  `nile library sync` outside Heroic reports "User not logged in".
- **GOG** has no library-sync CLI (gogdl only downloads).
- For GOG and Amazon, `tools/heroic_sync.sh` starts Heroic hidden for a few
  seconds so it syncs itself, then closes it.

### Gotchas
- No playtime in the library caches. Heroic writes playtime to
  `GamesConfig/<app_name>.json` only for games launched through it.
- Epic's hex `app_name` and Amazon's UUID mean nothing outside their store,
  so cross-store matching is by title (and year) through IGDB.

---

## Xodus Nexus (Xbox Game Pass on Linux)

### What it is
An Electron AppImage launcher for PC Game Pass with its own Wine-based
runtime.

### Where data lives
| Path | Contents |
|---|---|
| `~/.config/xodus-nexus/launcher-state.json` | Installed games and settings |
| `~/.config/xodus-nexus/xbox-profile.json` | Xbox profile and **auth tokens**: never read or copy |
| `~/.config/xodus-nexus/download-ownership/` | DRM license records, not a game list |
| `~/.local/share/xodus-nexus/` | Runtimes, compatdata, logs |

### What it gives you
`launcher-state.json` tracks **installed games only**. Fields per game:
`id`, `title`, `path`, `storeId`, `publisher`, `executable`,
`coverDataUrl`, `installState`, `installedVersion`.

The full Game Pass catalog comes from Microsoft's public catalog endpoints
instead (`importers/gamepass.py`, no sign-in needed).

### Gotchas
- The Game Pass catalog changes monthly.
- Xodus is early-stage, so its file format may change between versions.

---

## ES-DE (emulation front-end)

### Where it lives
- Gamelists: `~/ES-DE/gamelists/<system>/gamelist.xml`
- ROMs: `~/ROMs/<system>/` by default (ES-DE's `ROMDirectory` setting can move it)
- Artwork: `~/ES-DE/downloaded_media/<system>/<covers|miximages|screenshots|...>/`

### Fields in gamelist.xml
```
path           relative path to the ROM file
name           display title (scraped)
desc           description
rating         float 0.0–1.0
releasedate    8-digit date string
developer, publisher
genre          comma-separated, e.g. "Platform, Action"
players        e.g. "1" or "1-2"
favorite       "true" or absent
playcount      integer
playtime       seconds
lastplayed     e.g. "20261013T183812"
```

### Parser gotcha: two root elements
Systems with an alternative emulator set store a bare
`<alternativeEmulator>` element **before** `<gameList>`, which makes the
file invalid XML. `xml.etree.ElementTree` raises "junk after document
element". The importer strips everything outside `<gameList>…</gameList>`
before parsing:

```python
raw = open(path).read()
start = raw.find("<gameList")
end = raw.rfind("</gameList>") + len("</gameList>")
tree = ET.fromstring(raw[start:end])
```

### Freshness
ES-DE writes playtime back to `gamelist.xml` when it exits, so close ES-DE
before editing gamelists or it overwrites your changes. `playcount` and
`lastplayed` only exist for games launched through ES-DE.

---

## Lutris

### Where it lives
- Database: `~/.local/share/lutris/pga.db` (SQLite). Bazzite has no
  `sqlite3` command, so read it with Python's `sqlite3` module.
- Cover art: `~/.local/share/lutris/banners/` and `coverart/`

### Fields
`name`, `runner`, `platform`, `playtime` (hours, float), `lastplayed` (Unix
timestamp), `installed`, `year`, `service`, `service_id`

### Gotcha
Lutris is often launcher plumbing: Ubisoft Connect, EA App and Battle.net as
Wine wrappers. The games inside those launchers aren't in `pga.db`.

---

## Why a systemd user timer
Bazzite has no `crond` by default, and `systemd --user` is the native way on
Fedora Atomic. Logs go to `journalctl --user -u game-catalog-refresh`, and
the portal's Scans page shows each run's per-step logs.
