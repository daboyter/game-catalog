# DroidDeck Game Pass scan (Odin 2)

Checks every Game Pass title in the catalog against DroidDeck's Xbox store on
the Odin 2 (driven over adb from the Mac, screen read with macOS Vision OCR)
and records whether it is installable there.

**Result file:** `droiddeck-status.jsonl` — one line per Game Pass product:
`status` installable / findable (listed, no Install) / missing / unsure,
`source` search or library, the offer text ("AVAILABLE WITH XBOX GAME PASS
PREMIUM • 7.5 GB"), the date and DroidDeck version. Committed here as the
source of truth; `tools/droiddeck_import.py` loads it into the catalog on
every refresh and predicts a status for titles added to Game Pass since.

## Results 2026-10-23 (DroidDeck 0.6.0)

516 titles: 383 installable, 109 missing, 24 unclear, 0 listed-but-not-installable.

Compared with the Xodus Nexus package check on bazzite
(`tools/xodus_resolve.py`, no sign-in):

| Xodus says | DroidDeck | |
|---|---|---|
| own Windows package (369) | installable 350 | 95% |
| unsupported / no package / lookup error (84) | missing 69 | 82% |
| package via another product (63) | 30 installable, 26 missing | coin flip → "unclear" |

The "another product" group can't be predicted from Store data: Xbox's own
edition bundles (Forza Horizon 5, Avowed, Flight Simulator 2024) and about
half the Ubisoft titles really are absent from DroidDeck's catalog (checked by
eye: searching "Forza Horizon 5" returns only Gears 5), while other bundles,
CoD launchers and the rest of Ubisoft install fine.

## Running a scan

Needs the Odin 2 on adb with the screen on (Developer options → Stay awake)
and DroidDeck open. USB was unreliable; Wi-Fi adb works well:

    adb -s <usb-serial> tcpip 5555        # once, while USB works
    adb connect <odin-ip>:5555
    export ANDROID_SERIAL=<odin-ip>:5555

    swiftc -O ocr.swift -o ocr            # macOS Vision OCR helper (once)
    # titles.json: [[store_id, title], ...] for the gamepass rows, e.g. from bazzite:
    #   sqlite3 -json ~/.local/share/game-catalog/library.db \
    #     "SELECT store_id, store_title FROM ownership WHERE store = 'gamepass'" \
    #     | python3 -c 'import json,sys; print(json.dumps([[r["store_id"], r["store_title"]] for r in json.load(sys.stdin)]))' > titles.json
    python3 run.py                        # pass 1: Store search, ~18 s/title, resumable → results.jsonl
    python3 recheck.py                    # pass 2: non-installable titles again, under the
                                          # product name Xodus reports + the Library filter
                                          # (needs xodus.jsonl copied from bazzite)

Then merge (recheck wins), write `droiddeck-status.jsonl`, commit, and record
the scan for the portal's Scans page:

    ssh bazzite.local '~/game-catalog-venv/bin/python3 ~/game-catalog/runlog.py \
        droiddeck ok <started-iso> <log> "<summary>" --run <stamp> --trigger mac'

A full scan takes ~2.5 h + ~45 min for the recheck; don't use the Odin meanwhile.

## Safety

Only OCR-located controls are tapped: the Stores sidebar entry, the Xbox
store chip, the Store/Library tabs, the search box, result tiles (after OCR
confirms a results page with this query in the search box), and the detail
page's Back button. Text and Enter are only sent while the keyboard is up
(search box focused). Install / Play Steam / View on Xbox are never tapped.
