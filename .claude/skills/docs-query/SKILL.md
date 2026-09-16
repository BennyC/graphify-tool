---
name: docs-query
description: Answer questions about a tool from its ingested documentation graph in corpora/ (ArgoCD, Agent Gateway, and any other corpus listed by scripts/corpora.py). Use whenever the user asks how a third-party tool works, how to configure it, or what a docs page says, and a matching corpus exists.
argument-hint: [slug ...] <question>
model: opus
allowed-tools: Bash(uv run scripts/corpora.py *), Bash(graphify query *), Bash(graphify explain *), Bash(graphify path *), Bash(graphify god-nodes *), Read, Glob, Grep
---

# docs-query

Answer from the ingested documentation, with citations to the original URLs.
All commands run from the repo root.

Arguments: `$ARGUMENTS`

## 1. Pick the corpus

```
uv run scripts/corpora.py --json
```

Match the question to corpora by `slug`, `name` and `aliases`. Leading
arguments that equal a slug are explicit selections. Several corpora can
match one question (for example a question about deploying tool A with tool
B). Query each separately and merge.

- Corpus is `stale: true` (older than 90 days): still answer, but open with
  one line saying when it was ingested.
- No corpus matches: do not answer from memory. Say which corpora exist and
  propose the ingest command with your best guess at the docs URL, then stop:
  `/docs-ingest <slug> <url>`
- The user asks something the docs cannot answer (opinion, comparison with a
  tool not ingested): say what the docs cover, answer the rest from general
  knowledge, and label which is which.

## 2. Query the graph

Specific questions ("how do I configure X", "what does flag Y do"):

```
graphify query "<question>" --graph <graph_path> --budget 4000
```

Query matching is case-folded substring, no stemming or synonyms. Use the
tool's own vocabulary, not the user's paraphrase: `sync wave` not `ordering`,
`ApplicationSet` not `app set`. If the result is thin, retry with two or
three alternative terms, or use `graphify explain "<node label>"` on a hub.
`graphify god-nodes --top 15 --graph <graph_path>` shows the vocabulary.

Orientation questions ("how does X work overall", "what are the main
concepts"): read `<wiki_path>` first and follow the community articles it
links. Use the graph query only to drill into specifics.

## 3. Read the source before answering

Graph nodes carry `source_file`, relative to `<raw_path>/`. Open that file:
its frontmatter has `source_url` and `title`, and its body is the evidence.
`corpora/<slug>/pages.json` maps every file to its URL if you need a batch
lookup. For every claim you intend to make, confirm it in the raw page. The
graph tells you where to look; the page is the evidence. Never cite a URL
you did not read.

## 4. Answer

- Lead with the answer. Concrete config or commands go in fenced blocks,
  copied from the docs, not reconstructed.
- End with a `Sources` list: page title and `source_url` for every page you
  relied on. One line each.
- Mention graph structure (communities, related nodes, paths) only if the
  user asks for it.
- Multiple corpora: one section per corpus, then a short synthesis.

## Do not

- Answer from memory when a corpus exists for the tool.
- Rebuild or re-ingest anything. That is `/docs-ingest`.
- Cite `graphify-out/` files as sources. Sources are the original doc URLs.
