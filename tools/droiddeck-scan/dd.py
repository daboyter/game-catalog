"""
Drive DroidDeck's Xbox Store search on the Odin 2 over adb and read the
screen with macOS Vision OCR (./ocr). Coordinates are full-res 1920x1080.

Safety: the only things ever tapped are the search box, a result tile (only
after OCR confirms a results page is showing, never a detail page), and the
detail page's Back button. Install / View on Xbox are never tapped.
"""

import json, pathlib, re, subprocess, time, unicodedata, difflib

HERE = pathlib.Path(__file__).parent
SHOT = HERE / "shot.png"

SEARCH_BOX = (1030, 252)     # Store tab search field (once search is open)
BACK_BTN   = (350, 210)      # "< Back" on a detail page
RESULTS_TOP = 300            # result tiles are below this y


class DeviceGone(Exception):
    pass


def wait_for_device(timeout=600):
    """Block until adb sees the Odin 2 again; raise DeviceGone after `timeout` seconds."""
    end = time.time() + timeout
    while time.time() < end:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
        if any(l.endswith("\tdevice") for l in out.splitlines()[1:]):
            return
        time.sleep(10)
    raise DeviceGone("Odin 2 not connected over adb")


def adb(*args, check=True):
    r = subprocess.run(["adb", "shell", *args], capture_output=True, text=True)
    if r.returncode != 0 and "no devices" in (r.stderr + r.stdout):
        wait_for_device()
        r = subprocess.run(["adb", "shell", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, args, r.stdout, r.stderr)
    return r.stdout


def keyboard_up():
    return "mInputShown=true" in adb("dumpsys", "input_method")


def tap(xy):
    try:
        adb("input", "tap", str(xy[0]), str(xy[1]))
    except subprocess.CalledProcessError:            # occasional transient failure: retry once
        time.sleep(1)
        adb("input", "tap", str(xy[0]), str(xy[1]))


# Vision sometimes reads Latin letters as Cyrillic look-alikes ("< Baсk", "Еpic")
LOOKALIKES = str.maketrans("АВЕКМНОРСТХаеорсухіјЅѕ", "ABEKMHOPCTXaeopcyxijSs")


def screen():
    """OCR of the current screen: list of {text,x,y,w,h,conf}."""
    r = subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True)
    if r.returncode != 0:
        wait_for_device()
        r = subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True, check=True)
    png = r.stdout
    SHOT.write_bytes(png)
    out = subprocess.run([str(HERE / "ocr"), str(SHOT)], capture_output=True, text=True, check=True).stdout
    lines = [json.loads(l) for l in out.splitlines() if l.strip()]
    for l in lines:
        l["text"] = l["text"].translate(LOOKALIKES)
    return lines


def texts(lines):
    return [l["text"] for l in lines]


def on_detail(lines):
    """Detail pages have the '< Back' button at top-left."""
    return any(l["text"].strip().lower().endswith("back") and l["x"] < 450 and l["y"] < 260 for l in lines)


def norm(t):
    t = re.sub(r"[™®©]", "", t)
    t = unicodedata.normalize("NFKD", t).lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]", "", t)


def query_for(title):
    """Search text safe for `adb input text`: ASCII words only, max 6 words."""
    t = re.sub(r"['’™®©]", "", title)                   # Knight's → Knights; ™ before NFKD makes it "TM"
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = re.sub(r"[™®©]", "", t)
    t = re.sub(r"\((Windows|PC|Game Preview)\)|- Standard Edition|Standard Edition", "", t, flags=re.I)
    words = re.sub(r"[^A-Za-z0-9 ]", " ", t).split()
    return " ".join(words[:6])


def query_variants(title):
    """Full query, the part before a colon/dash subtitle, then the first three words."""
    full = query_for(title)
    main = query_for(re.split(r"\s*[:–—]\s*|\s+-\s+", title)[0])
    first3 = " ".join(full.split()[:3])
    out = []
    for q in (full, main, first3):
        if q and len(q) >= 3 and q not in out:
            out.append(q)
    return out


LIBRARY_TAB = (365, 219)
LIBRARY_FILTER = (936, 252)   # Library tab's own search field
STORE_TAB   = (528, 220)
SEARCH_ICON = (636, 219)


def scan_library(max_screens=20):
    """Scroll DroidDeck's Xbox Library grid and return every tile label seen."""
    tap(LIBRARY_TAB); time.sleep(2)
    # the Library has its own search filter, which can still hold an old query
    tap(LIBRARY_FILTER); time.sleep(0.6)
    adb("input", "keycombination", "113", "29"); adb("input", "keyevent", "67")
    time.sleep(1.5)
    if keyboard_up():
        adb("input", "keyevent", "4"); time.sleep(0.8)
    seen, stale = {}, 0
    for _ in range(max_screens):
        for t in result_tiles(screen()):
            seen.setdefault(t["text"], t["size"])
        before = len(seen)
        adb("input", "swipe", "1000", "950", "1000", "420", "600")   # scroll down ~2 rows
        time.sleep(1.5)
        new = len({t["text"] for t in result_tiles(screen())} - set(seen))
        stale = stale + 1 if new == 0 else 0
        if stale >= 2:
            break
    for t in result_tiles(screen()):
        seen.setdefault(t["text"], t["size"])
    scroll_to_top()                            # so the header (tabs, search) is visible again
    return seen


def header_label(lines, text):
    """The header label (above the results) whose text starts with `text`, or None."""
    return next((l for l in lines if l["y"] < 320 and l["text"].strip().startswith(text)), None)


def scroll_to_top():
    for _ in range(6):
        adb("input", "swipe", "1000", "450", "1000", "1000", "250")
    time.sleep(1)


def go_to_xbox_store(lines, attempt):
    """
    From wherever DroidDeck is (Steam home, Games, a scrolled grid, the downloads
    panel), get to the Xbox store page. Only OCR-located labels are tapped:
    the "Stores" sidebar entry and the "Xbox" store chip.
    """
    xbox = next((l for l in lines if l["y"] < 140 and l["text"].strip().endswith("Xbox")), None)
    stores = next((l for l in lines if l["x"] < 230 and l["text"].strip() == "Stores"), None)
    if xbox and attempt % 2 == 0:
        tap((xbox["x"] + xbox["w"] // 2, xbox["y"] + xbox["h"] // 2)); time.sleep(1.5)
    elif not xbox and stores:
        tap((stores["x"] + stores["w"] // 2, stores["y"] - 40)); time.sleep(2)   # icon above the label
    else:
        scroll_to_top()


def open_store_search():
    """
    Get to the Xbox Store tab with the search box focused (keyboard showing).
    Finds the tabs by OCR instead of trusting fixed positions: a scrolled grid
    pushes the header off-screen, and a fixed tap would then land on a game tile.
    Returns True only when the keyboard is up, i.e. the search box has focus.
    """
    for attempt in range(5):
        lines = screen()
        if on_detail(lines):
            tap(BACK_BTN); time.sleep(1.2); continue
        store = header_label(lines, "Store")
        if not store:                          # header scrolled away, or a panel (downloads) is open
            go_to_xbox_store(lines, attempt)
            continue
        tap((store["x"] + store["w"] // 2, store["y"] + store["h"] // 2)); time.sleep(1.2)
        lines = screen()
        store = header_label(lines, "Store")
        if not store:
            continue
        # search icon (closed) or the search field (open) sits just right of "Store"
        tap((store["x"] + store["w"] + 70, store["y"] + store["h"] // 2)); time.sleep(0.8)
        if keyboard_up():
            return True
        tap((1030, store["y"] + store["h"] // 2)); time.sleep(0.8)   # field already open: focus it
        if keyboard_up():
            return True
    return False


def search(q):
    """
    Type q into the Store search box and submit; returns OCR of the results, or
    None if the search box couldn't be focused (nothing is typed in that case).
    """
    if not open_store_search():
        return None
    adb("input", "keycombination", "113", "29")        # Ctrl+A
    adb("input", "keyevent", "67")                     # Delete
    adb("input", "text", q.replace(" ", "%s"))
    if not keyboard_up():                              # focus lost: don't press Enter anywhere else
        return None
    adb("input", "keyevent", "66")                     # Enter
    time.sleep(3.5)
    if keyboard_up():                                  # Back only hides the keyboard if it's showing;
        adb("input", "keyevent", "4")                  # otherwise it would navigate the app
        time.sleep(0.8)
    lines = screen()
    if on_detail(lines):
        return None
    # results belong to this query only if the search field shows it
    first = norm(q.split()[0]) if q.split() else ""
    if not any(l["y"] < 320 and first and first in norm(l["text"]) for l in lines):
        return None
    return lines


SIZE_RE = re.compile(r"^\d+(\.\d+)?\s*(GB|MB|KB)$")


def result_tiles(lines):
    """Tile title labels under the search bar (size labels and chrome removed)."""
    tiles = []
    for l in lines:
        t = l["text"].strip()
        if l["y"] < RESULTS_TOP or l["x"] < 230 or SIZE_RE.match(t) or len(t) < 2:
            continue
        m = re.match(r"^(.*?)\s+(\d+(?:\.\d+)?\s*(?:GB|MB|KB))$", t)   # "Title… 27.9 GB" read as one label
        if m:
            tiles.append({"text": m.group(1), "x": l["x"], "y": l["y"], "w": l["w"], "h": l["h"], "size": m.group(2)})
            continue
        size = next((m["text"] for m in lines if SIZE_RE.match(m["text"].strip())
                     and abs(m["y"] - l["y"]) < 20 and 0 < m["x"] - l["x"] < 420), None)
        tiles.append({"text": t, "x": l["x"], "y": l["y"], "w": l["w"], "h": l["h"], "size": size})
    return tiles


def tile_score(tile_text, title):
    """How well a (possibly truncated, '…') tile label matches the Game Pass title."""
    truncated = tile_text.endswith(("…", "...", ".."))
    a, b = norm(tile_text.rstrip("….")), norm(title)
    if not a or not b:
        return 0
    if a == b:
        return 1.0
    if truncated and len(a) >= 5 and b.startswith(a):
        return 0.9
    if len(a) >= 6 and (b.startswith(a) or a.startswith(b)):
        return 0.8
    return difflib.SequenceMatcher(None, a, b).ratio() * 0.85


def read_detail():
    """OCR the open detail page → {title, install, buttons, offer, lines}."""
    time.sleep(2.5)
    lines = screen()
    if not on_detail(lines):
        return None
    ts = texts(lines)
    install = next((t for t in ts if re.match(r"^(Install|Play|Update|Installed|Uninstall)\b", t.strip())), None)
    # the small caps line above the title: "AVAILABLE WITH XBOX GAME PASS PREMIUM • 152.7 GB",
    # "STORE • 169.4 GB", ...
    offer = next((t for t in ts if re.search(r"[•·]\s*\d+(\.\d+)?\s*(GB|MB|KB)", t)
                  or "GAME PASS" in t.upper() or "AVAILABLE" in t.upper() or "OWNED" in t.upper()), None)
    # title candidates: hero-card lines, plus consecutive pairs for titles that wrap
    hero = sorted((l for l in lines if 250 < l["y"] < 600 and 230 < l["x"] < 1200), key=lambda l: l["y"])
    cands = [l["text"] for l in hero] + [a["text"] + " " + b["text"] for a, b in zip(hero, hero[1:])]
    return {"title": None, "title_candidates": cands, "install": install, "offer": offer,
            "buttons": [t for t in ts if t.strip() in ("View on Xbox", "Buy", "Get", "Not available")],
            "lines": ts[:40]}


def back():
    tap(BACK_BTN); time.sleep(1.2)


def best_score(text, names):
    return max(tile_score(text, n) for n in names)


def check(title, max_open=3, aliases=()):
    """
    Classify one Game Pass title → dict with status installable / findable / missing.
    aliases: other names the game may appear under in DroidDeck, e.g. the product
    Xodus says actually delivers it ("Forza Horizon 5" for "... Standard Edition").
    """
    names = [title] + [a for a in aliases if a and a != title]
    queries = []
    for n in names:
        for q in query_variants(n):
            if q not in queries:
                queries.append(q)
    for q in queries:                          # full title, then shorter forms
        lines = search(q)
        if lines is None:                      # search box not reachable / results not for q
            return {"title": title, "query": q, "status": "error", "why": "search not confirmed"}
        tiles = sorted(result_tiles(lines), key=lambda t: -best_score(t["text"], names))
        cands = [t for t in tiles if best_score(t["text"], names) >= 0.75][:max_open]
        if cands:
            break
    if not cands:
        return {"title": title, "query": q, "status": "missing",
                "results": [t["text"] for t in tiles][:10]}
    for t in cands:
        tap((t["x"] + 20, t["y"] - 60))        # tile body, above its label
        d = read_detail()
        if d is None:
            continue
        # confirm on the detail page: a cut-off tile ("Age of Empire…") can be a different game
        best = max(d["title_candidates"] or [""], key=lambda c: best_score(c, names))
        d["title"] = best
        same = best_score(best, names) >= 0.85 or best_score(t["text"], names) == 1.0
        back()
        if not same:
            continue
        status = "installable" if d["install"] else "findable"
        return {"title": title, "query": q, "status": status, "tile": t["text"], "size": t["size"],
                "detail_title": d["title"], "install": d["install"], "offer": d["offer"],
                "buttons": d["buttons"], "detail_lines": d["lines"] if status == "findable" else None}
    return {"title": title, "query": q, "status": "unsure",
            "results": [t["text"] for t in tiles][:10], "tried": [t["text"] for t in cands]}


# ── second pass: the Library tab's own filter ─────────────────────────────────
# Store search misses some games that ARE in the account's DroidDeck Library
# (Age of Empires III, Assassin's Creed Origins), so missing/unsure titles are
# rechecked against the Library.

def open_library_filter():
    """Library tab with its filter field focused (keyboard up); True on success."""
    for attempt in range(5):
        lines = screen()
        if on_detail(lines):
            tap(BACK_BTN); time.sleep(1.2); continue
        lib = header_label(lines, "Library")
        if not lib:
            go_to_xbox_store(lines, attempt)
            continue
        tap((lib["x"] + lib["w"] // 2, lib["y"] + lib["h"] // 2)); time.sleep(1.5)
        lines = screen()
        lib = header_label(lines, "Library")
        if not lib:
            continue
        tap((LIBRARY_FILTER[0], lib["y"] + lib["h"] // 2)); time.sleep(0.8)
        if keyboard_up():
            return True
    return False


def check_library(title, max_open=4):
    """installable (in Library, page has Install/Play) / findable (in Library, no button) / missing."""
    for q in query_variants(title):
        if not open_library_filter():
            return {"title": title, "query": q, "status": "error", "why": "library filter not reachable"}
        adb("input", "keycombination", "113", "29"); adb("input", "keyevent", "67")
        adb("input", "text", q.replace(" ", "%s"))
        time.sleep(2.5)                                 # the Library filters as you type
        if keyboard_up():
            adb("input", "keyevent", "4"); time.sleep(0.8)
        lines = screen()
        if on_detail(lines):
            continue
        tiles = sorted(result_tiles(lines), key=lambda t: -tile_score(t["text"], title))
        cands = [t for t in tiles if tile_score(t["text"], title) >= 0.75][:max_open]
        for t in cands:
            tap((t["x"] + 20, t["y"] - 60))
            d = read_detail()
            if d is None:
                continue
            best = max(d["title_candidates"] or [""], key=lambda c: tile_score(c, title))
            back()
            if tile_score(best, title) >= 0.85 or tile_score(t["text"], title) == 1.0:
                return {"title": title, "query": q, "status": "installable" if d["install"] else "findable",
                        "source": "library", "tile": t["text"], "size": t["size"], "detail_title": best,
                        "install": d["install"], "offer": d["offer"]}
    return {"title": title, "status": "missing", "source": "library"}


def clear_library_filter():
    if open_library_filter():
        adb("input", "keycombination", "113", "29"); adb("input", "keyevent", "67")
        adb("input", "keyevent", "4"); time.sleep(0.8)
