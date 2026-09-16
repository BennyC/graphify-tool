# Docs Graph Tool

Ingest a documentation website into a Graphify knowledge graph, then query it
locally from Claude Code. One corpus per tool (ArgoCD, Agent Gateway, ...).

Decisions below came from a grilling session on 2026-09-16. Change them here
before changing the code.

## Where it lives

- This repo, local git, not pushed for now.
- Skills are project-scoped in `.claude/skills/`. They load only when `claude`
  runs from this repo root. Nothing is installed into `~/.claude`.
- The only machine-level install is `uv tool install graphifyy` and `uv` itself.
- `corpora/` is gitignored. When the repo is shared, delete that line so
  teammates get prebuilt graphs instead of paying extraction again.

## Layout

```
.claude/skills/docs-ingest/SKILL.md   explicit, model: sonnet
.claude/skills/docs-query/SKILL.md    auto-invocable, model: opus
scripts/                              Python, inline uv deps, run from repo root
corpora/<slug>/
  manifest.json                       identity, source, dates, counts, cost
  raw/                                one markdown file per page, source_url frontmatter
  pages.json                          file -> url map
  scrape.json                         scrape summary (strategy, fetched_at, capped)
  graphify-out/                       Graphify output, cache kept across re-ingests
```

## Ingest: `/docs-ingest <slug> <url>`

1. Source strategy. The scraper tries these automatically, in order:
   1. `llms.txt` at the site root, used only as a seed list of page URLs.
      `llms-full.txt` is ignored because it loses page boundaries and
      therefore citations.
   2. `sitemap.xml` (from robots.txt, the origin, or the prefix), filtered
      to the URL prefix.
   3. Crawl under the URL prefix by following links.
   Cloning the docs source repo is a fourth strategy the skill offers
   explicitly (`--repo`, `--docs-path`, `--ref`) rather than trying it
   automatically, because picking the repo needs Claude and the user in the
   loop and rendered pages proved more faithful than raw MkDocs source.
   Manifest records `source_repo`, `source_ref`, `docs_path` when used.
   If a site serves native markdown at `<url>.md` the scraper uses it
   instead of converting HTML (Agent Gateway does; ArgoCD does not).
2. Scraper writes each page as markdown with `source_url` and `title`
   frontmatter, plus `pages.json`. Main-content extraction via trafilatura,
   markdownify fallback. Polite delay, descriptive user agent.
3. Report page count. Default cap 500 (`--max-pages` overrides). If over the
   cap, stop and ask before extraction. Scraping is cheap, extraction is not.
4. Extraction runs headless from inside the corpus dir:
   `graphify extract raw/ --backend claude-cli`. This shells out to `claude -p`
   and bills the user's subscription. Chunks run serially by default.
   `GRAPHIFY_CLAUDE_CLI_MODEL` pins the extraction model. Then community
   labelling with the same backend and `graphify export wiki`.
   Fallback if the CLI backend breaks: install the official `/graphify` skill
   and run it with cwd set to the corpus dir.
5. Write `manifest.json`: slug, name, aliases, source_url, strategy, repo
   fields if cloned, version, ingested_at, page_count, cost from
   `graphify-out/cost.json`.
6. Re-ingest replaces `raw/` in full but keeps `graphify-out/cache/` so
   unchanged pages cost nothing.

## Query: `/docs-query [slug ...] <question>`

1. Read every `corpora/*/manifest.json`. Match the question to corpora by
   name and aliases, or use explicit slugs. No separate registry.
2. Specific questions: `graphify query "<q>" --graph corpora/<slug>/graphify-out/graph.json`,
   then read the cited raw pages. Orientation questions: navigate
   `graphify-out/wiki/index.md`. Budget defaults to Graphify's 2000 tokens,
   the skill may raise it.
3. Answer with citations to original doc URLs. Graph structure only when
   asked. Several corpora are queried separately and merged.
4. Warn if `ingested_at` is older than 90 days.
5. No matching corpus: propose the ingest command with a guessed URL and
   stop. Never answer from memory without saying so.

## Not doing

- MCP server per corpus. Extra process and config churn.
- `graphify claude install`. Its CLAUDE.md section and hooks are for
  "graphify the repo you're in" and would fire in every project.
- Incremental scrape. Full replace plus Graphify's semantic cache is enough.
- Hardcoded table of tool -> repo. Goes stale.
