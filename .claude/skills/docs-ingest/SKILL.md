---
name: docs-ingest
description: Scrape a documentation website into a corpus and build its Graphify knowledge graph. Explicit only; costs time and Claude subscription tokens.
argument-hint: <slug> <docs-url-prefix> [--name "..."] [--aliases a,b] [--version v] [--max-pages N]
disable-model-invocation: true
model: sonnet
allowed-tools: Bash(uv run *), Bash(graphify *), Bash(git clone *), Bash(curl *), Read, Glob, Grep, WebFetch
---

# docs-ingest

Ingest a docs site into `corpora/<slug>/` and build a Graphify graph over it.
Read `SPEC.md` if anything here is unclear. All commands run from the repo root.

Arguments: `$ARGUMENTS`

## 1. Parse the request

- `slug`: lowercase, hyphens, e.g. `argocd`, `agentgateway`. If the user gave
  a name instead of a slug, derive one and say which you chose.
- `url`: the docs prefix. Only pages under it are ingested. Versioned sites
  need the version in the prefix, e.g. `https://agentgateway.dev/docs/kubernetes/latest/`
  not `https://agentgateway.dev/docs/`. If the user gave a bare site root,
  fetch it and look for the versioned docs path, then propose the prefix.
- Propose `--name`, `--aliases` (short forms people actually type: `argo`,
  `argo-cd`, `ag`) and `--version` if the user did not give them. Aliases
  drive corpus matching in `/docs-query`, so include the obvious ones.
- If `corpora/<slug>/manifest.json` already exists this is a re-ingest. Say so
  and show the existing `ingested_at`. Re-ingest replaces `raw/` in full but
  keeps Graphify's cache, so unchanged pages are free.

## 2. Discover before you spend

```
uv run scripts/ingest.py --slug <slug> --url <url> --skip-extract --max-pages 500
```

This scrapes only. It first runs discovery and exits with code 3 and an
`over_cap` JSON if the site has more pages than the cap. In that case stop and
ask the user: narrow the prefix, raise `--max-pages`, or add `--truncate`.
Do not pick for them; extraction cost scales with page count.

If the site has no `llms.txt` links and no sitemap, the scraper falls back to
link crawling. Say so, because crawl counts are unknown until the run ends.

Repo strategy: when the site is hard to scrape (no sitemap, JS-rendered, or
the user prefers source), find the docs source repo from the site's
"edit this page" link or footer, propose it, and once confirmed use:

```
uv run scripts/ingest.py --slug <slug> --url <url> --repo <git-url> --docs-path docs --ref <branch>
```

## 3. Report the scrape, then extract

After the scrape-only run, read the JSON summary and report: strategy,
page count, whether native markdown was used, skipped and error counts.
Spot-check one file in `corpora/<slug>/raw/` for garbage (nav menus,
tooltips inlined into prose, empty pages). If it looks wrong, fix the prefix
or strategy before extracting.

Then build the graph, reusing the scrape:

```
uv run scripts/ingest.py --slug <slug> --url <url> --name "<name>" --aliases <a,b> --version <v> --skip-scrape
```

Extraction shells out to `claude -p` and bills the user's Claude subscription.
Defaults: model `sonnet`, serial chunks. Roughly 2 minutes per chunk of 20 to
25 pages. For a 400-page site that is 30 to 40 minutes serial. `--parallel 4`
speeds it up; `--model haiku` makes it cheaper at some quality cost. Tell the
user the estimate before running. Run it in the background and poll; do not
block the session on it.

## 4. Confirm the result

- Read `corpora/<slug>/manifest.json`. Report page count, node and edge
  counts, token usage, and whether the wiki exported.
- Run `graphify god-nodes --top 8 --graph corpora/<slug>/graphify-out/graph.json`
  and show the hubs. If they are navigation junk ("Edit this page", "Table
  of contents") the scrape needs cleaning; say so rather than shipping it.
- Suggest one test question for `/docs-query <slug> ...`.

## Do not

- Run `graphify claude install` or `graphify install`. They touch the user's
  global config.
- Ingest without reporting the page count first.
- Guess a versioned prefix silently.
