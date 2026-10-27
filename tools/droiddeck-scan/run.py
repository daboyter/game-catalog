"""
Check every Game Pass title in DroidDeck. Resumable: results.jsonl gets one
line per title and finished titles are skipped on restart.
    python3 run.py [title substring ...]   # optional: only matching titles
"""
import json, pathlib, subprocess, sys, time, traceback
import dd

HERE = pathlib.Path(__file__).parent
OUT = HERE / "results.jsonl"
SHOTS = HERE / "shots"; SHOTS.mkdir(exist_ok=True)

titles = json.loads((HERE / "titles.json").read_text())
only = [a.lower() for a in sys.argv[1:]]
if only:
    titles = [t for t in titles if any(o in t[1].lower() for o in only)]
done = {json.loads(l)["store_id"] for l in OUT.read_text().splitlines()} if OUT.exists() else set()
todo = [t for t in titles if t[0] not in done]
print(f"{len(todo)} to check ({len(done)} already done)", flush=True)

start = time.time()
for i, (store_id, title) in enumerate(todo, 1):
    for attempt in (1, 2):
        try:
            r = dd.check(title)
            break
        except dd.DeviceGone:
            print("Odin 2 disconnected for 10 minutes — stopping; rerun to resume", flush=True)
            sys.exit(2)
        except Exception as e:
            r = {"title": title, "status": "error", "why": repr(e)[:200]}
            time.sleep(3)
    r["store_id"] = store_id
    if r["status"] in ("missing", "unsure", "error", "findable"):
        # keep a small screenshot of the results page for review
        subprocess.run(["sips", "-Z", "960", "-s", "format", "jpeg", str(dd.SHOT),
                        "--out", str(SHOTS / f"{store_id}.jpg")], capture_output=True)
    with OUT.open("a") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    rate = (time.time() - start) / i
    print(f"[{i}/{len(todo)}] {r['status']:11s} {title[:50]}  ({rate:.1f}s each, "
          f"~{rate * (len(todo) - i) / 60:.0f} min left)", flush=True)
