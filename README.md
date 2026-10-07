# reddit-intel

Token-efficient Reddit intelligence CLI, built for LLM agents.

Fetches Reddit discussions through free community archives — no API key, no
account, no cost — and compresses them **before** they reach the agent's
context (~40:1 token saving vs raw JSON).

## Install

```bash
git clone https://github.com/longyunBegin/reddit-intel.git
cd reddit-intel
./bin/reddit-intel --help
```

Requirements: Python 3.10+, standard library only. No dependencies.

## Usage

```bash
# Search posts (compact table by default)
./bin/reddit-intel search "1.6T optical" --subs semiconductors,hardware --since 7d

# Top comments of a post
./bin/reddit-intel comments 1wwfn3n --top 20

# Briefing prep for a time window (agent writes the actual summary)
./bin/reddit-intel digest "AI datacenter" --subs stocks --since 24h --limit 15
```

### Flags

| Flag | Description | Default |
|---|---|---|
| `--subs a,b,c` | subreddits to search | `all` |
| `--since` | `24h`, `7d`, `30d` or `YYYY-MM-DD` | `7d` |
| `--limit N` | max posts returned | `10` |
| `--min-score N` | minimum post score (search) | `0` |
| `--excerpt-chars N` | excerpt length | `200` |
| `--format` | `compact` (markdown table) or `json` | `compact` |
| `--backend` | `auto`, `arctic-shift`, `pullpush` | `auto` |

## How token saving works

1. **Field allowlist** — 8 fields kept (`id`, `title`, `subreddit`, `score`,
   `num_comments`, `created`, `url`, `excerpt`), 30+ dropped
2. **Excerpt truncation** — body cut at word boundary, not mid-word
3. **Server-side ranking** — gravity score (`score / (age_hours + 2)^1.5`),
   top N only
4. **Compact rendering** — markdown table instead of repeated-key JSON
5. **Progressive depth** — `search` → `comments` → `digest`; drill down only
   as needed

The CLI never calls an LLM. Semantic work (clustering, sentiment, summaries)
belongs to the calling agent — the tool's job is to turn 50k tokens of raw
data into 1k tokens of prepared material.

## Data sources

| Backend | Role | Auth |
|---|---|---|
| [Arctic Shift](https://arctic-shift.photon-reddit.com) | primary | none |
| [PullPush](https://api.pullpush.io) | fallback | none |

Three-level search fallback: Arctic Shift full-text → PullPush full-text →
Arctic Shift browse + client-side keyword filter. Transient failures are
retried with backoff. Both are volunteer-run archives; ~1 req/sec pacing is
enforced — please be polite.

## Exit codes

- `0` — ok
- `1` — usage error (bad flags)
- `2` — all backends failed

## Limits

- Archive data lags live Reddit (~1 hour); not for second-level monitoring
- Public posts and comments only (no auth endpoints)

## For agents

See [SKILL.md](SKILL.md) for the agent-facing usage guide.

## License

MIT
