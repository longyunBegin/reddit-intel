---
name: "reddit-intel"
description: "Token-efficient Reddit intelligence for agents: search posts, fetch top comments, and build topic digests via free archive APIs (Arctic Shift / PullPush, no key needed). Use when the user asks to monitor Reddit, gauge sentiment, or pull discussions on any topic."
---

# reddit-intel

Reddit intelligence CLI built for LLM agents. Free data, no API key, and
output compressed *before* it reaches your context (~40:1 token saving vs raw JSON).

## Quick start

```bash
bin/reddit-intel search "1.6T optical" --subs semiconductors,hardware --since 7d
bin/reddit-intel comments <post_id> --top 20
bin/reddit-intel digest "AI datacenter" --subs stocks --since 24h
```

## Commands

**`search <query>`** — ranked post list (compact table: title, sub, score,
comments, date, 200-char excerpt, URL). Use first; drill down only into
posts worth reading.

**`comments <post_id>`** — top comments by score, bodies truncated. Accepts
id with or without `t3_` prefix.

**`digest <query>`** — briefing prep for a time window: counts + ranked posts
with excerpts. YOU (the agent) write the actual summary — the CLI only
prepares the material, keeping zero-LLM-dependency.

## Useful flags

- `--subs a,b,c` — subreddits (default: `all`)
- `--since 24h|7d|30d|YYYY-MM-DD` — time window (default: `7d`)
- `--limit N` — max posts (default: 10)
- `--min-score N` — filter low-engagement posts (search only)
- `--format json` — machine-readable 8-field objects (default: compact table)
- `--backend auto|arctic-shift|pullpush` — default `auto` with fallback

## How token saving works

1. Field allowlist: 8 fields kept, 30+ dropped
2. Excerpt truncation at word boundary (default 200 chars)
3. Server-side gravity ranking (score x time-decay), top N only
4. Markdown table instead of repeated-key JSON
5. Progressive depth: search -> comments -> digest

The CLI never calls an LLM. Semantic work (clustering, sentiment) is yours.

## Reliability

Three-level search fallback: Arctic Shift full-text -> PullPush full-text ->
Arctic Shift browse + client-side keyword filter. Transient failures retried
with backoff; be patient, these are volunteer-run archives (~1 req/sec pacing
is enforced). Exit codes: 0 ok, 1 usage error, 2 all backends failed.

## Limits

- Archive data lags live Reddit (~1h); not for second-level monitoring
- No auth endpoints: public posts/comments only
