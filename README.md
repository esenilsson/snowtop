# snowtop

A terminal UI for **watching Snowflake queries** — like Snowsight's Query History, in your
terminal. Live watch of what's running right now, plus a history browser over any time window.

- **Live mode** — RUNNING / QUEUED queries, auto-refreshing.
- **History mode** — every query in a time window, all statuses (Success/Failed/…), sortable.
- **Select any row** → detail pane with the **full, syntax-highlighted SQL** plus metadata
  (target table, dbt model/env, warehouse, role, timings, spill/lock warnings, error).

## Why it's cheap

It reads the `INFORMATION_SCHEMA.QUERY_HISTORY()` table function, which runs in Snowflake's
**cloud-services layer and uses no warehouse compute credits**. Both the live watch and the
history browser are effectively free. (`SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` has ~45 min
latency and can't show what's running now, so it isn't used.) `snowtop` is **strictly
read-only** with respect to account data — it only reads metadata and sets a query tag on its
own private session so its monitoring queries can be hidden from the overview.

## Requirements

- [`uv`](https://docs.astral.sh/uv/) — manages the Python version and deps for you.
- A Snowflake connection in `~/.snowflake/connections.toml` (you already have `DK95507`).

## Run

```bash
# try the UI with fake data, no connection needed:
uv run snowtop --demo

# the real thing (opens SSO browser once, then caches the token):
uv run snowtop                 # live TUI
uv run snowtop --history       # start in history mode (last 1 day)
uv run snowtop --history --since 4h

# include Snowtop's own metadata queries (they are hidden by default):
uv run snowtop --show-snowtop-queries
```

Install a global `snowtop` command and add uv's tool directory to your shell `PATH`:

```bash
uv tool install .
uv tool update-shell           # adds ~/.local/bin to your shell startup file

# Open a new terminal, then this works from anywhere:
snowtop
```

If `snowtop` is still not found, restart the terminal or run `uv tool update-shell` again and
follow its printed instructions for your shell.

## Keys (in the TUI)

| Key | Action |
|---|---|
| ↑ / ↓ | move the row cursor (updates the detail pane) |
| `Enter` | lock/unlock the current query in the detail pane while live results refresh |
| `h` | toggle **live ↔ history** |
| `s` | cycle status filter (All → Running → Queued → Success → Failed) |
| `f` | jump to the filter box (type to match user or SQL; Enter returns to the table) |
| `d` / `t` / `u` | sort by duration / start time / user (press again to reverse) |
| *click a column header* | sort by that column |
| `r` | reload now |
| `q` | quit |

The **DURATION** column shows a bar scaled to the slowest visible query, colored by status,
with a `⚠` when the query spilled to storage (a sign the warehouse is undersized).

## Scriptable snapshot

For piping/cron, `--once` prints a table and exits (no TUI):

```bash
uv run snowtop --once                        # ongoing queries
uv run snowtop --once --history --since 1d   # last day
```

## Whose queries you see (scope)

Scope follows your **role's privileges**: a role with account-wide **MONITOR** (e.g.
`ACCOUNTADMIN`) sees every user's queries; any other role sees only your own. Override the role
with `-r/--role`:

```bash
uv run snowtop -r ACCOUNTADMIN
```

## All options

```
-c, --connection   named connection from connections.toml (default: your default)
-r, --role         override role (use one with MONITOR to see all users)
    --history      start in history mode instead of live
    --since DUR    history window: 30m / 4h / 1d (default 1d)
    --limit N      max rows to fetch (default 1000)
-n, --interval     live auto-refresh seconds (default 3; 0 disables)
    --warn N       flag queries running longer than N seconds (default 300)
    --show-snowtop-queries
                 include Snowtop's own metadata queries
    --once         print one snapshot and exit (no TUI)
    --demo         synthetic data, no Snowflake connection (try the UI)
```

## Notes

- First run opens the browser once for SSO; `keyring` then caches the token so later runs
  reuse it.
- Your `connections.toml` (what the Python connector reads) has no role set, so it uses your
  default role — which resolves to `ACCOUNTADMIN`, giving account-wide visibility. Pin a
  different role with `-r` if you prefer.
