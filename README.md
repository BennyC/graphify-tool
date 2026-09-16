# Docs Graph Tool

Turn a documentation website into a local Graphify knowledge graph, then ask
questions about it from Claude Code. See `SPEC.md` for the design decisions.

## Setup

```
uv tool install graphifyy      # the graphify CLI
cd <this repo> && claude       # skills load from .claude/skills/ when run here
```

Nothing is installed into `~/.claude`. Extraction bills your Claude
subscription via `claude -p`.

## Use

```
/docs-ingest argocd https://argo-cd.readthedocs.io/en/stable/ --name "Argo CD" --aliases argo,argo-cd
/docs-query how do sync waves interact with hooks in argocd
uv run scripts/corpora.py     # list what is ingested
```

`/docs-query` also triggers on its own when you ask about a tool that has a
corpus.

## Layout

```
corpora/<slug>/manifest.json   identity, source, dates, counts, cost
corpora/<slug>/raw/            one markdown file per page, source_url frontmatter
corpora/<slug>/pages.json      file -> url map
corpora/<slug>/scrape.json     scrape summary (strategy, fetched_at, cap)
corpora/<slug>/graphify-out/   graph.json, GRAPH_REPORT.md, wiki/, cache/
scripts/scrape.py              site -> markdown (llms.txt, sitemap, or crawl; native .md if served)
scripts/ingest.py              scrape + graphify extract + label + wiki + manifest
scripts/corpora.py             list corpora with staleness
```

`corpora/` is gitignored. Delete that line in `.gitignore` to ship prebuilt
graphs to teammates.
