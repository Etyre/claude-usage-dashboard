# Claude usage dashboard

A local, auto-updating dashboard of what you spend on Claude: tokens and dollars per day,
per app (API key or Agent SDK), per model, per billing source, plus a separate view of your own
interactive Claude Code sessions. Built 2 November 2026 because no single Anthropic view covers
both API-key spend and the subscription-billed usage of Claude Code and Agent SDK apps running
on this machine. Python 3 standard library only; the page uses Chart.js from cdnjs with no build step.

```
./install.sh            # start at login and keep running (macOS launchd)
open http://localhost:8787
```

Or run it in the foreground: `python3 server.py`. No dependencies beyond Python 3.

## How it works

`server.py` is a single process: a `ThreadingHTTPServer` on `127.0.0.1:<port>` serving
`index.html`, `/api/summary` (the whole in-memory snapshot as JSON) and `/api/refresh` (kick every
source now), plus one background thread per data source that re-pulls on its own interval and
writes what it fetched to `cache/`. The page polls `/api/summary` and does all grouping and
charting client-side. Days are bucketed in the configured timezone starting at `day_start_hour`
(default 4 am America/Los_Angeles); the cache files record which timezone/day-start they were
built with and are discarded when that changes.

### Data sources

| Tab | Source | Freshness | Cost |
|---|---|---|---|
| Apps (API key) | Anthropic **Usage and Cost Analytics API**, grouped by API key, model, product, workspace | ~4 hours (Anthropic's export cadence) | Actual billed amounts (post-discount) |
| Apps (API key), today | Anthropic **legacy usage report** (token counts only) | ~5 minutes | Estimated from list prices until billing data covers the day |
| Apps (Agent SDK) | Transcripts the Claude Agent SDK leaves under `~/.claude/projects` (records whose `entrypoint` is `sdk-py` / `sdk-ts` / `sdk-cli`), plus per-call logs apps write to `~/.claude/app-usage/*.jsonl` (below) | 60 seconds | Subscription usage, valued at list prices |
| Claude Code sessions | `~/.claude/projects/**/*.jsonl` transcripts of interactive sessions on this machine | 60 seconds | Estimated at list prices (matches Claude Code's own `/cost`) |

Apps built on the Agent SDK usually sign in with the Claude subscription rather than an API key,
so no Anthropic API reports them. The SDK still writes a transcript per session, which the Apps
tab reads; the app is named after the SDK's working directory (a generic folder such as `data`
falls back to its parent, temp dirs become "Ad hoc"). An app that turns session persistence off,
or wants to be counted without transcripts, appends one JSON line per call to
`~/.claude/app-usage/<app>.jsonl`:

```json
{"ts": "2026-11-06T19:52:55-08:00", "app": "Focus-monitor", "source": "agent-sdk", "billing": "subscription",
 "model": "claude-opus-5", "usage": {"input_tokens": 2, "output_tokens": 231, "cache_read_input_tokens": 1327,
 "cache_creation_input_tokens": 22519}}
```

`usage` takes the Anthropic usage fields (`cache_creation` with `ephemeral_5m_input_tokens` /
`ephemeral_1h_input_tokens` is honoured; a bare `cache_creation_input_tokens` is priced as 5-minute
writes). Name the app the same as its API key so both sources merge into one row.

No double counting with the Analytics API: an SDK session that ran on an API key is recognised
(Claude Code writes a `credential_org` attachment into the transcript only for login sessions),
and such transcripts, like log lines with `"billing": "api"`, are left out of the Apps tab when
an Admin key is configured, since the Analytics API already reports them with billed cost.

### Admin API key

Both API sources need an **Admin API key** (`sk-ant-admin01-…`):

1. Claude Console → [Settings → Admin keys](https://platform.claude.com/settings/admin-keys) → Create admin key.
   Admin keys exist only for organizations. If you are on an individual account, create a (free)
   organization first at [Settings → Organization](https://platform.claude.com/settings/organization).
2. `cp .env.example .env` and paste the key in.
3. Restart (`./install.sh` again, or Ctrl-C and rerun).

If you use Claude Code with an API key from the same organization, it also shows up in the
API tab under product `claude_code`. Claude Code billed to a Pro/Max subscription is not
reported by any API; the local tab covers that.

Without a key the dashboard still runs and shows the two local sources only.

### Pricing

List prices per model live in the `PRICING` table at the top of `server.py` (USD per million
tokens: input, output, cache read, 5-minute cache write, 1-hour cache write; longest model-name
prefix wins). They are only used for the estimated columns; the Analytics API supplies billed cost.
Update the table when Anthropic changes prices or ships a new model.

## Files

| File | Purpose |
|---|---|
| `server.py` | The whole backend: config, Admin API client, transcript and app-log parsers, cache, background refresh threads, HTTP server, `--test` self-check |
| `index.html` | The dashboard page (Chart.js from cdnjs, no build step); polls `/api/summary` |
| `com.elityre.claude-usage-dashboard.plist` | launchd agent template; `install.sh` fills in `__ROOT__`, `__PYTHON__`, `__LOG__` |
| `install.sh` | Renders the plist into `~/Library/LaunchAgents`, (re)starts the agent. Idempotent |
| `uninstall.sh` | Stops the agent and removes the installed plist |
| `.env.example` | Template for `.env` (`ANTHROPIC_ADMIN_KEY`, optional `PORT`) |
| `config.example.json` | Template for `config.json` (all knobs with their defaults) |
| `.env`, `config.json` | Your real secret and settings; gitignored |
| `cache/` | Fetched data and parse offsets (`analytics_days.json`, `names.json`, `local_files_v4.json`, `app_logs.json`); gitignored, rebuilt if deleted |

## Runtime locations

| What | Where |
|---|---|
| Installed launchd agent | `~/Library/LaunchAgents/com.elityre.claude-usage-dashboard.plist` (label `com.elityre.claude-usage-dashboard`) |
| Code it runs | This folder, in place (`server.py` is not copied anywhere) |
| Config | `.env` and `config.json`, looked up first in `~/Library/Application Support/claude-usage-dashboard/`, then in this folder. The copies in this folder are the ones in use today |
| State | `cache/` in this folder |
| Logs | `~/Library/Logs/claude-usage-dashboard.log` (launchd); stdout/stderr in the foreground. Every line is timestamped. Installs before 7 Nov 2026 logged to `server.log` in this folder; rerun `./install.sh` to move |
| Inputs it reads | `~/.claude/projects/*/*.jsonl` (transcripts), `~/.claude/app-usage/*.jsonl` (app logs) |
| URL | `http://localhost:8787` (or `port` in `config.json` / `PORT` in `.env`) |

## Common tasks

```sh
cd ~/Documents/agent-coding/claude-usage-dashboard

./install.sh                       # install, or restart on new code / new .env (idempotent)
./uninstall.sh                     # stop and remove the launchd agent
python3 server.py                  # run in the foreground instead (Ctrl-C to stop)
PORT=8799 python3 server.py        # foreground on another port, beside the installed instance

python3 server.py --test           # end-to-end check: runs every source once, prints ok/ERROR
                                   # per source and SELF-TEST OK/FAILED, exits 1 on failure.
                                   # Opens no port, so it is safe while the agent is running.
curl -s localhost:8787/api/summary | python3 -m json.tool | head -40   # what the page sees
curl -s localhost:8787/api/refresh                                     # re-pull everything now

tail -f ~/Library/Logs/claude-usage-dashboard.log
launchctl print gui/$(id -u)/com.elityre.claude-usage-dashboard | grep -E 'state|pid'
launchctl kickstart -k gui/$(id -u)/com.elityre.claude-usage-dashboard   # restart without reinstalling
rm -rf cache/                      # force a full refetch on next start
```

## Settings that matter

Copy `config.example.json` to `config.json`. Useful knobs: `timezone` and `day_start_hour` (days are
bucketed in this IANA zone and start at this hour, default 4 am America/Los_Angeles; changing either
discards the cache and refetches), `port`, `history_days` (how far back to backfill, max 365 and no
earlier than 2026-01-01), and `app_names` to relabel API keys with your app names (keys are matched
by API key id, by the key's Console name, by an app-usage log's `app` field, or by an Agent SDK
working directory, full path or path suffix). Other knobs: `revisit_days` (recent days re-fetched
each refresh because Anthropic revises them), `analytics_refresh_min`, `live_refresh_min`,
`local_refresh_sec`, `claude_projects_dir`, `app_usage_dir`.

Changes to `config.json` or `.env` take effect on restart (`./install.sh`).

Outside this project:

- The Admin key must belong to an organization (Claude Console). Individual accounts have no Admin keys.
- Claude Code must keep writing transcripts under `~/.claude/projects` (the default) for the local tabs to see anything.

## Gotchas learned

- HTTP 403 "Missing permissions" from the Admin API (seen 2 Nov 2026 with a fresh key) means the key
  lacks the usage/billing scope or is not an org Admin key; the server logs it once per refresh and
  keeps serving the local tabs.
- A key that is organization-scoped but not admin can read the legacy usage report yet gets 401/403/404
  from the Analytics API. The server detects that, switches `status.analytics.mode` to `legacy` and
  estimates cost from list prices instead of failing.
- The Analytics API lags by about four hours and revises recent days, which is why `revisit_days`
  re-fetches the last 10 days every refresh; do not treat them as final.
- Restart with `./install.sh` or `launchctl kickstart -k`, not `pkill python3`: under launchd the
  process shows up as `.../Python.app/Contents/MacOS/Python server.py`, so a name match on `python3`
  silently kills nothing and the old code keeps running.
- The caches are keyed by logical day, so changing `timezone` or `day_start_hour` discards them on
  purpose; the first refresh after that re-parses every transcript.
- `python3 server.py --test` and the launchd instance may both write `cache/`; that is safe because
  every write is atomic (`.tmp` + rename) and the running instance only reads the cache at startup.
