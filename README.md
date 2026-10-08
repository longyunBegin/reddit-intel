# reddit-intel

Token-efficient Reddit intelligence CLI, built for LLM agents.

Fetches Reddit discussions through the [Arctic Shift](https://arctic-shift.photon-reddit.com)
archive — no API key, no account, no cost — and compresses them **before** they
reach the agent's context.

## Install

```bash
git clone https://github.com/longyunBegin/reddit-intel.git
cd reddit-intel
./bin/reddit-intel --help
```

Requirements: Python 3.10+, standard library only. No dependencies.

## Usage

`--subs` is required. Arctic Shift returns an empty 200 for `subreddit=all` and
400 for a full-text query with no subreddit, so a missing subreddit is a usage
error (exit 1), not an empty digest.

```bash
# Search posts in a real time window (compact table)
./bin/reddit-intel search "1.6T optical" --subs semiconductors,hardware --since 7d

# One query, several aliases: results merge, posts hit by more aliases rank higher
./bin/reddit-intel search "optical interconnect|CPO|1.6T" --subs semiconductors,hardware --since 7d

# Recent posts ranked by live comments/hour (archive scores are stale for ~36h)
./bin/reddit-intel hot --subs stocks --since 24h --limit 10
./bin/reddit-intel search "earnings" --subs stocks --since 24h --engagement

# Top comments of a post
./bin/reddit-intel comments 1wwfn3n --top 20

# Briefing prep for a time window (the agent writes the summary)
./bin/reddit-intel digest "AI datacenter" --subs stocks --since 24h --limit 15
```

### Flags

| Flag | Description | Default |
|---|---|---|
| `--subs a,b` | subreddits to search; **required**. `all` is rejected | none |
| `--since` | `24h`, `7d`, `30d`, or `YYYY-MM-DD` (UTC) | `7d` (`36h` for `hot`) |
| `--limit N` | max posts returned | `10` |
| `--min-score N` | minimum score for posts older than 36h | `0` |
| `--min-comments N` | minimum comment count (posts younger than 36h kept regardless) | `0` |
| `--include-removed` | keep posts whose body is `[removed]`/`[deleted]` (dropped by default) | off |
| `--excerpt-chars N` | excerpt length (search, digest, hot) | `200` |
| `--format` | `compact` (markdown table) or `json` | `compact` |
| `--backend` | `auto`, `arctic-shift`, or `pullpush` | `auto` |
| `--engagement` | live comment counts for posts younger than 36h | off (`hot` turns it on) |
| `--engagement-top N` | how many young posts to measure (max 12) | `8` (`10` for `hot`) |

`search` takes a query (`""` browses the window). `hot` takes an optional query.
`a|b|c` splits the query into aliases: each alias runs its own full-text
pass and the results merge (posts matched by several aliases rank higher).
Within one alias every term must match (quoted phrases stay one term), the
way the server does it. Keyword filtering uses word boundaries, so `1.6T`
no longer matches `$6 trillion`; English plurals still match (`s?`); CJK
terms fall back to substring matching. The Arctic Shift `query` parameter
is sent through unchanged; if that index errors or returns nothing, a
single shared browse of the same window is filtered per alias the same way.

### Score pending and live engagement

Arctic Shift ingests a post at creation time. `score` and `num_comments` are
backfilled about **36 hours** later, so younger posts usually show score 1 and
0 comments. The compact table prints `pending` instead of that snapshot.
`num_comments` on those rows is `?` until it is measured.

`--engagement` and `hot` then:

1. take up to N young posts (spread across the window, not only the last minutes)
2. count comments with `/api/comments/search?link_id=` (`fields=id,created_utc`, no bodies)
3. rank those posts by comments per hour (age floored at 15 minutes)
4. rank posts older than 36h by gravity on the real score: `score / (age_hours + 2) ^ 1.5`

Counts above 300 are a lower bound (`300+`). Results are cached for 15 minutes
under `$XDG_CACHE_HOME/reddit-intel/engagement.json` (or `~/.cache/...`).
Set `REDDIT_INTEL_CACHE=off` to disable the file. Requests run on 4 workers
through pooled keep-alive connections (proxies honored via `*_proxy` env
vars) behind a shared adaptive rate limiter: brisk when the archive is
healthy, backing off on 429s and slow-query timeouts.

### How the time window works

`--since` is sent as an integer epoch `after`. Full-text queries scan one
window per subreddit. The unfiltered browse fallback splits the window into
day-sized slices that scan in parallel; slices of one sub share the
**1000 posts per subreddit** budget so slicing never multiplies the fetch
cap. A page starts at 100 posts; a 422 timeout retries that page at 50,
then 25, because a full page of bodies on a busy sub times out. A truncated
window is called out in the output; narrow `--since` or `--subs` to finish
it. Ranking happens after that scan, not on the newest handful of rows.

## How token saving works

1. **Field allowlist** — `id`, `title`, `subreddit`, `score`, `score_status`,
   `num_comments`, `created`, `url`, `excerpt`. Live rows also carry
   `comments_per_hour` and `comments_source`. A removed body sets `removed`
   and an empty excerpt.
2. **Excerpt truncation** — body cut at a word boundary (`--excerpt-chars`)
3. **Ranking** — gravity for backfilled posts; comments/hour when `--engagement`
   measured a young post. Top N only.
4. **Compact rendering** — markdown table, ASCII headers (safe on GBK consoles)
5. **Progressive depth** — `search` / `hot` → `comments` → `digest`

The CLI never calls an LLM.

## Data sources

| Backend | Role | Auth |
|---|---|---|
| [Arctic Shift](https://arctic-shift.photon-reddit.com) | only automatic source | none |
| [PullPush](https://api.pullpush.io) | explicit `--backend pullpush` only | none |

`auto` does not call PullPush. PullPush responds to agent clients with a
refusal (429 "does not provide free scraping resources for agents", or a 403
HTML block) and that response is not retried.

Full-text search failures, and queries that return zero rows, fall back **once**
to an Arctic Shift browse of the same window plus a client-side term filter.

HTTP 429 waits for `X-RateLimit-Reset` (capped at 45s). HTTP 400/404 and
unrecognized HTTP 422 responses fail immediately; only Arctic Shift's
`Timeout. Maybe slow down a bit` 422 uses jittered backoff before the page is
retried smaller. 5xx use jittered backoff. Nothing sleeps after the attempt
that gives up. Please stay polite; these are volunteer archives.

## Exit codes

- `0` — ok, including a real empty window
- `1` — usage error (missing `--subs`, bad flags, argparse errors)
- `2` — the backend failed

## Tests

```bash
python3 -m unittest discover -s tests
```

## Limits

- Post scores and stored comment counts lag ~36 hours. Comment text is much
  closer to live. Not a second-level monitor.
- At most 1000 posts are scanned per subreddit per command. Busy subs over a
  long `--since` need a narrower window.
- Public posts and comments only.

## For agents

See [SKILL.md](SKILL.md).

## License

MIT
