# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""List ingested corpora. Run from the repo root: uv run scripts/corpora.py [--json]

Prints slug, name, aliases, page count, graph size, age in days and a STALE flag
when ingested_at is older than 90 days.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

STALE_DAYS = 90
root = Path(__file__).resolve().parent.parent / "corpora"
rows = []
for m in sorted(root.glob("*/manifest.json")):
    d = json.loads(m.read_text())
    age = None
    if d.get("ingested_at"):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(d["ingested_at"])).days
    d["age_days"] = age
    d["stale"] = age is not None and age > STALE_DAYS
    d["graph_path"] = str(m.parent / "graphify-out" / "graph.json")
    d["wiki_path"] = str(m.parent / "graphify-out" / "wiki" / "index.md") if d.get("wiki") else None
    d["raw_path"] = str(m.parent / "raw")
    rows.append(d)

if "--json" in sys.argv:
    print(json.dumps(rows, indent=2))
elif not rows:
    print("no corpora ingested yet. Use /docs-ingest.")
else:
    for d in rows:
        g = d.get("graph", {})
        flag = "  STALE" if d["stale"] else ""
        print(f"{d['slug']:<16} {d.get('name',''):<24} pages={d.get('page_count')}  nodes={g.get('nodes')} edges={g.get('edges')}  "
              f"age={d['age_days']}d  aliases={','.join(d.get('aliases', []))}{flag}")
        print(f"{'':<16} {d.get('source_url','')}")
