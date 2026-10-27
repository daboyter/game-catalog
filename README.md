# game-catalog

One searchable library of every game you own or can play, across every
launcher on a Linux gaming PC. It gathers Steam, Epic, GOG and Amazon (through
Heroic), Lutris, Xbox Game Pass and your ES-DE emulation library into one
SQLite database. It matches everything to [IGDB](https://www.igdb.com) for
genres, themes, ratings, art and time-to-beat, then serves it as:

- **A web portal** for browsing on any device on your network. You can filter
  by store, genre, theme, mode, rating or length, open game and ROM pages
  with "if you liked this" picks, and track your own play status and ratings.
- **An MCP server**, so Claude (or another MCP client) can answer "what should
  I play tonight?" from your actual library.

![The library grid: each card shows the year, IGDB rating, time to beat and every store you have the game on](docs/screenshots/browse.png)

![A game page: IGDB details, time to beat, your status and rating, where you can play it, and related picks](docs/screenshots/game-page.png)

I built it for my own setup: a 2018 Mac mini running [Bazzite](https://bazzite.gg),
plus an AYN Odin 2 Portal handheld (Android) that plays some Game Pass titles
through DroidDeck. The Odin-specific parts are optional, and everything else
should work on any Linux box where these launchers keep their default paths.

> **Personal project, shared as-is.** I'm not taking issues or pull requests,
> and I can't help with setup, but you're welcome to fork it and make it yours.
> It was written with [Claude Code](https://claude.com/claude-code), and the
> comments explain the why as well as the what, so it should read well even
> if you only borrow ideas from it.

## What it reads

| Source | How | What you need |
|---|---|---|
| Steam | Steam Web API (`importers/steam.py`) | An API key and your SteamID64 in `steam.env` |
| Epic, GOG, Amazon Prime Gaming | Heroic's library caches (`importers/heroic.py`); Epic is refreshed headlessly with Heroic's bundled `legendary` | Heroic (Flatpak), signed in to each store |
| Lutris | `~/.local/share/lutris/pga.db` (`importers/lutris.py`) | Lutris, if you use it |
| Xbox Game Pass | Microsoft's public catalog endpoints, no sign-in (`importers/gamepass.py`); installed titles from Xodus Nexus (`importers/xodus.py`) | Nothing for the catalog; Xodus Nexus for installed titles |
| ES-DE ROMs | `~/ES-DE/gamelists/*/gamelist.xml` and downloaded media (`importers/esde.py`, `match_roms.py`) | ES-DE |
| IGDB | Twitch-authenticated IGDB API (`merge.py`, `enrich_details.py`, `match_roms.py`) | A Twitch application's client ID and secret in `igdb.env` |

Any source you don't have is skipped. [`docs/data-sources.md`](docs/data-sources.md)
has the details of where each launcher keeps its data, and the gotchas.

## Setup

Everything runs as your user: no root, no containers. That suits Bazzite and
other immutable distros.

```bash
# 1. The code, in ~/game-catalog (the systemd units expect this path)
git clone https://github.com/daboyter/game-catalog.git ~/game-catalog

# 2. A Python venv with the two packages (Flask and the MCP SDK)
python3 -m venv ~/game-catalog-venv
~/game-catalog-venv/bin/pip install -r ~/game-catalog/requirements.txt

# 3. Credentials (see the comments in each example for where to get them)
mkdir -p ~/.config/game-catalog
cp ~/game-catalog/steam.env.example ~/.config/game-catalog/steam.env
cp ~/game-catalog/igdb.env.example  ~/.config/game-catalog/igdb.env
chmod 600 ~/.config/game-catalog/*.env
#    ...then edit both files

# 4. First import (the IGDB matching takes a while on a big library)
bash ~/game-catalog/refresh.sh

# 5. The portal on port 5000, and a weekly refresh
mkdir -p ~/.config/systemd/user
cp ~/game-catalog/game-catalog-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now game-catalog-portal.service game-catalog-refresh.timer
loginctl enable-linger "$USER"     # keep them running when you're not logged in
```

Then open `http://<your-host>:5000`.

To use a venv somewhere else, set `GAME_CATALOG_VENV` for `refresh.sh` and
change the paths in the `.service` files.

### Where things live

| What | Path |
|---|---|
| The database (rebuilt by each refresh) | `~/.local/share/game-catalog/library.db` |
| API caches and per-run logs | `~/.local/share/game-catalog/cache/`, `logs/` |
| Your play status and ratings (edited in the portal) | `~/.local/share/game-catalog/user/my-games.json` |
| Recommendation settings (edited in the portal) | `~/.local/share/game-catalog/settings/recommendations.json` |
| Credentials and your own lists | `~/.config/game-catalog/` |

### Your own lists

Four plain-text TSV files let you correct and annotate the catalog. Each file
in this repo is an empty template, and its comments explain the columns. Put
your copy in `~/.config/game-catalog/` (same file name) and the scripts read
that one instead, so your lists stay out of the repo.

| File | What it does |
|---|---|
| `hidden-games.tsv` | Store entries to leave out entirely |
| `igdb-pins.tsv` | Force a store game to a specific IGDB match |
| `rom-igdb-pins.tsv` | The same for ES-DE ROMs |
| `compat-notes.tsv` | Your own per-device notes (works / partial / broken, Proton version, notes) |

`not-games.tsv` (soundtracks, SDKs, launchers and other store entries that
aren't games) ships with a shared default. A copy in `~/.config/game-catalog/`
replaces it.

## The MCP server

`mcp_server.py` gives an MCP client four read-only tools: `describe_schema`,
`query_library` (read-only SQL), `search_games` and `my_games`. With Claude
Code on another machine, it runs over SSH:

```bash
claude mcp add --scope user game-library -- ssh <your-host> \
    '~/game-catalog-venv/bin/python3' '~/game-catalog/mcp_server.py'
```

## Optional: DroidDeck on the Odin 2

`tools/droiddeck-scan/` drives the DroidDeck app on an Android handheld over
`adb` from a Mac, and reads the screen with macOS Vision OCR. It records
which Game Pass titles DroidDeck can install. The result,
`droiddeck-status.jsonl`, is committed, and `tools/droiddeck_import.py` loads
it into the portal's "DroidDeck (Odin 2)" filter. Between scans,
`tools/xodus_resolve.py` predicts the status of new titles from their PC
packages. See [its README](tools/droiddeck-scan/README.md). If you don't have
the hardware, ignore it; the portal's Odin 2 columns just stay empty.

## Security

The portal has **no sign-in** and listens on all interfaces (`0.0.0.0:5000`).
Anyone who can reach it can see your library, change your ratings and start a
rescan. Keep it on your home network or behind something like Tailscale, and
don't forward the port on your router. Never commit the `.env` files (the
`.gitignore` blocks them).

## Credits and terms

- Game data, art and ratings come from [IGDB](https://www.igdb.com) (powered
  by IGDB.com). Use of the IGDB API follows the
  [Twitch Developer Services Agreement](https://www.twitch.tv/p/legal/developer-agreement/).
- Steam data comes from the [Steam Web API](https://steamcommunity.com/dev),
  under the [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms).
- This project isn't affiliated with Valve, Epic, GOG, Amazon, Microsoft,
  Twitch/IGDB, Heroic, Lutris, Xodus Nexus or ES-DE.

## License

[MIT](LICENSE). No warranty, as the license says.
