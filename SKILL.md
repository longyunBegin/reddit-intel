---
name: "reddit-intel"
description: "Token-efficient Reddit intelligence for agents. Search posts, rank recent threads by live comments/hour, fetch top comments, and build digests from the Arctic Shift archive (no API key). Use when the user asks to monitor Reddit, gauge sentiment, or pull discussions. Always pass --subs; scores younger than 36h are pending until --engagement or hot."
---

# reddit-intel

Reddit intelligence CLI for LLM agents. Archive data, no API key, output
compressed before it reaches your context.

## Quick start

```bash
bin/reddit-intel search "1.6T optical" --subs semiconductors,hardware --since 7d
bin/reddit-intel hot --subs stocks --since 24h --limit 10
bin/reddit-intel search "earnings" --subs stocks --since 24h --engagement
bin/reddit-intel comments <post_id> --top 20
bin/reddit-intel digest "AI datacenter" --subs stocks --since 24h
```

`--subs` is required. Do not omit it and do not pass `--subs all`. Arctic Shift
returns an empty 200 for `subreddit=all` and 400 when a query has no subreddit.
That used to look like "no discussion" with exit code 0. It is now exit code 1.

## Commands

**`search <query>`** — posts in the `--since` window, best first. Compact
table: title, sub, score, comments, date, excerpt, URL. A query of `""`
browses the window. Multi-word queries must match every term (`"quoted phrase"`
stays one term).

**`hot [query]`** — same window scan, but posts younger than 36h are ranked by
live comments/hour. Default `--since` is `36h`. This is the right command for
"what is active right now".

**`comments <post_id>`** — top comments by score, bodies truncated. Accepts an
id with or without the `t3_` prefix. `[removed]` / `[deleted]` bodies are
skipped (their replies are kept).

**`digest <query>`** — counts plus ranked posts with excerpts for one window.
You write the summary. The CLI does not call an LLM.

## Flags that change the result

- `--subs a,b` — required. Prefixes `r/` and `/r/` are accepted. Names must
  match `[A-Za-z0-9_]{2,30}`.
- `--since 24h|7d|30d|YYYY-MM-DD` — real server-side window (`after` + paging),
  not "the newest 20 posts". Capped at 1000 posts per subreddit; the output
  says `truncated: yes` when that happens. Narrow the window.
- `--engagement` — opt in on `search` and `digest`. Measures up to
  `--engagement-top` young posts (default 8, hard max 12) via the comment
  search endpoint, caches counts for 15 minutes, paces at ~1 request / 1.5s.
- `--min-score` — applies only to posts older than 36h. Pending scores are not
  real yet, so they are kept.
- `--excerpt-chars` — excerpt length for search, hot, and digest (default 200).
- `--format json` — the compact table's fields plus `score_status`.
- `--backend pullpush` — explicit opt-in only. `auto` never calls PullPush.
  PullPush refuses agent traffic; do not use it as a fallback.

## How to read scores

Arctic Shift copies `score` and `num_comments` at creation and backfills them
about 36 hours later. Until then the archive value is usually score 1 and
0 comments, which makes gravity ranking collapse to "newest first".

- Compact score cell `pending` means not backfilled yet. Do not treat it as 1.
- Comment cell `?` means the archive count is not live. `4?` means the archive
  already showed a non-zero count but it is still inside the 36h window.
- With `--engagement` / `hot`, measured rows show an integer count (or `300+`)
  and a `/h` column. Posts older than 36h keep the real score and are ranked
  by gravity: `score / (age_hours + 2)^1.5`.
- A title suffix `[removed]` means the body was removed or deleted. The excerpt
  is empty. Posts whose title itself is `[removed]` or `[deleted]` are dropped.

The stdout comment line reports what was actually scanned:

```text
<!-- backend: arctic-shift | scanned: 144 | posts: 10 | oldest: 2026-10-06T16:06:27Z | truncated: no -->
```

`oldest` near the start of `--since` and `truncated: no` means the window was
covered before ranking.

## Reliability

`auto` uses Arctic Shift only. If full-text search errors or returns zero
rows, the same window is browsed once and filtered on the client. That browse
is not repeated when it is also empty.

Retries: 429 waits for `X-RateLimit-Reset` (cap 45s); 400/404 fail immediately;
422 timeouts and 5xx use jittered backoff. The process does not sleep again
after the final failure. Base pace is about 1 request/second.

Exit codes: `0` ok (including a genuinely empty window), `1` usage error,
`2` backend failure.

## Limits

- Not a live Reddit socket. Comment ingestion is close to real time; scores are not.
- Public posts and comments only.
- Engagement is an extra few requests. Keep `--engagement-top` small.
