"""
Second pass over DroidDeck results that aren't "installable": search again also
under the name of the product Xodus says delivers the game, then check the
Library filter. Writes recheck.jsonl (resumable); final merge happens later.
"""
import json, pathlib, sys, time
import dd

HERE = pathlib.Path(__file__).parent
OUT = HERE / "recheck.jsonl"
xo = {r["store_id"]: r for r in map(json.loads, open(HERE / "xodus.jsonl"))}
first = {r["store_id"]: r for r in map(json.loads, open(HERE / "results.jsonl"))}
done = {json.loads(l)["store_id"] for l in OUT.read_text().splitlines()} if OUT.exists() else set()
todo = [r for r in first.values() if r["status"] != "installable" and r["store_id"] not in done]
print(f"{len(todo)} to recheck", flush=True)
for i, r in enumerate(todo, 1):
    aliases = [d["title"] for d in xo.get(r["store_id"], {}).get("desktop", []) if d.get("title")]
    try:
        res = dd.check(r["title"], aliases=aliases)
        if res["status"] in ("missing", "unsure"):
            lib = dd.check_library(r["title"])
            if lib["status"] in ("installable", "findable"):
                res = lib
    except dd.DeviceGone:
        print("Odin 2 disconnected — stopping; rerun to resume", flush=True); sys.exit(2)
    except Exception as e:
        res = {"title": r["title"], "status": "error", "why": repr(e)[:200]}
    res["store_id"] = r["store_id"]; res["first_status"] = r["status"]; res["aliases"] = aliases
    with OUT.open("a") as f:
        f.write(json.dumps(res, ensure_ascii=False) + "\n")
    print(f"[{i}/{len(todo)}] {r['status']:>8s} -> {res['status']:11s} {r['title'][:50]}", flush=True)
