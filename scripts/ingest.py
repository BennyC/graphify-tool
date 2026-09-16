# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Ingest a documentation site into corpora/<slug>/ and build its Graphify graph.

Run from the repo root:
    uv run scripts/ingest.py --slug argocd --url https://argo-cd.readthedocs.io/en/stable/ \
        --name "Argo CD" --aliases argo,argo-cd,argocd --version stable

Steps:
    1. discover  scrape.py --dry-run; stop with exit 3 if the site is over --max-pages
                 unless --truncate is given
    2. scrape    scrape.py into corpora/<slug>/raw (full replace)
    3. extract   graphify extract raw --backend claude-cli --out .   (cwd = corpus dir)
    4. label     graphify label . --backend claude-cli --missing-only
    5. wiki      graphify export wiki
    6. manifest  corpora/<slug>/manifest.json (pages.json and scrape.json sit beside it)

--repo <git-url> --docs-path <dir> replaces step 2 with a shallow clone: markdown
files under <dir> are copied into raw/ and given source_url values derived from
--url using MkDocs conventions (index.md -> dir/, foo.md -> foo/).

Extraction shells out to `claude -p` and bills the user's Claude subscription.
--model sets GRAPHIFY_CLAUDE_CLI_MODEL (haiku, sonnet, opus or a full ID).
--parallel N opts into concurrent chunks (GRAPHIFY_CLAUDE_CLI_PARALLEL=1).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPORA = REPO_ROOT / "corpora"


def log(msg: str) -> None:
    print(f"[ingest] {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd) + (f"   (cwd={cwd})" if cwd else ""))
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=capture, check=False)


def scrape(args, out: Path, dry_run: bool) -> dict:
    cmd = ["uv", "run", "--quiet", str(REPO_ROOT / "scripts" / "scrape.py"), "--url", args.url, "--out", str(out),
           "--max-pages", str(args.max_pages), "--strategy", args.strategy]
    if args.follow_links:
        cmd.append("--follow-links")
    if dry_run:
        cmd.append("--dry-run")
    p = run(cmd, capture=True)
    sys.stderr.write(p.stderr)
    if p.returncode != 0:
        log(f"scrape failed with exit {p.returncode}")
        sys.exit(p.returncode)
    return json.loads(p.stdout)


def scrape_repo(args, out: Path) -> dict:
    """Shallow-clone a docs repo and copy markdown into raw/ with synthesized source_url."""
    prefix = args.url if args.url.endswith("/") else args.url + "/"
    with tempfile.TemporaryDirectory() as td:
        cmd = ["git", "clone", "--depth", "1", "--quiet"]
        if args.ref:
            cmd += ["--branch", args.ref]
        cmd += [args.repo, td]
        p = run(cmd)
        if p.returncode != 0:
            sys.exit(p.returncode)
        ref = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=td, text=True, capture_output=True).stdout.strip()
        src = Path(td) / args.docs_path
        if not src.is_dir():
            log(f"docs path not found in repo: {args.docs_path}")
            sys.exit(2)
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        pages = []
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for md in sorted(src.rglob("*.md")):
            rel = md.relative_to(src)
            if len(pages) >= args.max_pages:
                break
            text = md.read_text(encoding="utf-8", errors="replace")
            url_path = rel.as_posix()
            if url_path.endswith(("index.md", "README.md")):
                url_path = url_path.rsplit("/", 1)[0] + "/" if "/" in url_path else ""
            else:
                url_path = url_path[:-3] + "/"
            url = prefix + url_path
            m = re.search(r"^#\s+(.+)$", text, re.M)
            title = m.group(1).strip() if m else rel.stem
            if text.startswith("---"):
                # keep existing frontmatter body, but prepend ours
                text = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f"---\nsource_url: {url}\ntitle: {json.dumps(title)}\nfetched_at: {fetched_at}\n---\n\n{text}", encoding="utf-8")
            pages.append({"file": rel.as_posix(), "url": url, "title": title, "chars": len(text)})
        (out.parent / "pages.json").write_text(json.dumps(pages, indent=2), encoding="utf-8")
        summary = {"strategy": "repo", "pages": len(pages), "capped": len(pages) >= args.max_pages,
                   "source_ref": ref, "fetched_at": fetched_at, "skipped": 0, "errors": 0}
        (out.parent / "scrape.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        return summary


def frontmatter_fetched_at(raw: Path) -> str | None:
    """Recover fetched_at from any raw page when scrape.json is absent (pre-scrape.json corpora)."""
    for p in sorted(raw.rglob("*.md")):
        m = re.search(r"^fetched_at:\s*(\S+)$", p.read_text(encoding="utf-8", errors="replace")[:600], re.M)
        if m:
            return m.group(1)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slug", required=True, help="Corpus id, lowercase, e.g. argocd")
    ap.add_argument("--url", required=True, help="Docs URL prefix; only pages under it are ingested")
    ap.add_argument("--name", help="Display name, e.g. 'Argo CD' (default: slug)")
    ap.add_argument("--aliases", default="", help="Comma-separated aliases used for query matching")
    ap.add_argument("--version", default="", help="Docs version string for the manifest, e.g. stable, v3.1")
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--truncate", action="store_true", help="If over the cap, ingest the first --max-pages pages instead of stopping")
    ap.add_argument("--strategy", choices=["auto", "llms", "sitemap", "crawl"], default="auto")
    ap.add_argument("--follow-links", action="store_true")
    ap.add_argument("--repo", help="Git URL of the docs source; replaces scraping")
    ap.add_argument("--docs-path", default="docs", help="Directory inside --repo holding the markdown")
    ap.add_argument("--ref", help="Branch or tag for --repo")
    ap.add_argument("--model", default=os.environ.get("GRAPHIFY_CLAUDE_CLI_MODEL", "sonnet"),
                    help="Model for claude -p extraction (default sonnet)")
    ap.add_argument("--parallel", type=int, default=1, help="Concurrent extraction chunks (default 1)")
    ap.add_argument("--skip-scrape", action="store_true", help="Reuse existing raw/")
    ap.add_argument("--skip-extract", action="store_true", help="Scrape only, no Graphify")
    ap.add_argument("--force", action="store_true", help="Ignore Graphify's semantic cache")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.slug):
        log("slug must be lowercase letters, digits and hyphens")
        return 2
    if not shutil.which("graphify") and not args.skip_extract:
        log("graphify not on PATH. Install with: uv tool install graphifyy")
        return 2

    corpus = CORPORA / args.slug
    raw = corpus / "raw"
    corpus.mkdir(parents=True, exist_ok=True)
    manifest_path = corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    # 1 + 2: content
    if args.skip_scrape:
        if not (corpus / "pages.json").exists():
            log("--skip-scrape given but corpora/<slug>/pages.json is missing")
            return 2
        pages = json.loads((corpus / "pages.json").read_text())
        summary_path = corpus / "scrape.json"
        if summary_path.exists():
            scrape_info = json.loads(summary_path.read_text())
        else:
            scrape_info = {"strategy": manifest.get("strategy", "unknown"), "capped": manifest.get("capped", False),
                           "fetched_at": manifest.get("fetched_at") or frontmatter_fetched_at(raw)}
        scrape_info["pages"] = len(pages)
        log(f"reusing raw/: {len(pages)} pages ({scrape_info.get('strategy')}, fetched {scrape_info.get('fetched_at')})")
    elif args.repo:
        scrape_info = scrape_repo(args, raw)
        log(f"copied {scrape_info['pages']} markdown files from {args.repo}")
    else:
        plan = scrape(args, raw, dry_run=True)
        log(f"discovery: strategy={plan['strategy']} discovered={plan['discovered']} cap={args.max_pages}")
        if plan.get("over_cap") and not args.truncate:
            print(json.dumps({"status": "over_cap", **plan}, indent=2))
            log("over the page cap. Re-run with a larger --max-pages, a narrower --url, or --truncate.")
            return 3
        # keep Graphify's cache across re-ingests; raw/ itself is replaced by scrape.py
        scrape_info = scrape(args, raw, dry_run=False)
        log(f"scraped {scrape_info['pages']} pages ({scrape_info['strategy']}, native_md={scrape_info.get('native_markdown')})")

    if args.skip_extract:
        print(json.dumps({"status": "scraped", "slug": args.slug, **scrape_info}, indent=2))
        return 0

    # 3: extract
    env = dict(os.environ)
    env["GRAPHIFY_CLAUDE_CLI_MODEL"] = args.model
    if args.parallel > 1:
        env["GRAPHIFY_CLAUDE_CLI_PARALLEL"] = "1"
    cmd = ["graphify", "extract", "raw", "--backend", "claude-cli", "--out", ".", "--max-concurrency", str(args.parallel)]
    if args.force:
        cmd.append("--force")
    log(f"extracting {scrape_info['pages']} pages with claude-cli model={args.model} parallel={args.parallel}. This bills your Claude subscription.")
    tokens_in = tokens_out = None
    log("$ " + " ".join(cmd) + f"   (cwd={corpus})")
    proc = subprocess.Popen(cmd, cwd=corpus, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stderr.write(line)
        sys.stderr.flush()
        m = re.search(r"tokens:\s*([\d,]+) in / ([\d,]+) out", line)
        if m:
            tokens_in, tokens_out = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    proc.wait()
    if proc.returncode != 0:
        log(f"graphify extract failed with exit {proc.returncode}")
        return proc.returncode
    graph = corpus / "graphify-out" / "graph.json"
    if not graph.exists():
        log("graphify-out/graph.json missing after extract")
        return 1

    # 4: labels (extract clusters and labels already; this fills any placeholders)
    p = run(["graphify", "label", ".", "--backend", "claude-cli", "--missing-only"], cwd=corpus, env=env)
    if p.returncode != 0:
        log("community labelling failed, continuing with placeholder labels")

    # 5: wiki
    p = run(["graphify", "export", "wiki", "--graph", "graphify-out/graph.json"], cwd=corpus, env=env)
    if p.returncode != 0:
        log("wiki export failed, continuing")

    # 6: manifest
    g = json.loads(graph.read_text())
    edges = g.get("links", g.get("edges", []))
    communities = len({n.get("community") for n in g.get("nodes", []) if n.get("community") is not None})
    aliases = sorted({a.strip().lower() for a in args.aliases.split(",") if a.strip()} | {args.slug} | set(manifest.get("aliases", [])))
    manifest.update({
        "slug": args.slug,
        "name": args.name or manifest.get("name") or args.slug,
        "aliases": aliases,
        "source_url": args.url,
        "strategy": scrape_info.get("strategy"),
        "version": args.version or manifest.get("version", ""),
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched_at": scrape_info.get("fetched_at"),
        "page_count": scrape_info.get("pages"),
        "capped": scrape_info.get("capped", False),
        "graph": {"nodes": len(g.get("nodes", [])), "edges": len(edges), "communities": communities},
        "extraction": {"backend": "claude-cli", "model": args.model, "parallel": args.parallel,
                       "input_tokens": tokens_in, "output_tokens": tokens_out,
                       "note": "tokens are for this run only; cached pages cost nothing on re-ingest"},
        "wiki": (corpus / "graphify-out" / "wiki" / "index.md").exists(),
    })
    if args.repo:
        manifest.update({"source_repo": args.repo, "source_ref": scrape_info.get("source_ref"), "docs_path": args.docs_path})
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {manifest_path.relative_to(REPO_ROOT)}")
    print(json.dumps({"status": "ok", **{k: manifest[k] for k in ("slug", "name", "page_count", "graph", "ingested_at", "wiki")}}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
