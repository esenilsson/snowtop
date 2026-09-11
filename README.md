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

- [`uv`](https://docs.astral.sh/uv/) — it installs Python 3.11+ and dependencies.
- A Snowflake account and user that can authenticate from this machine.
- A connection profile in `~/.snowflake/connections.toml`.
- `USAGE` on at least one database. Snowtop needs a database selected to call the
  `INFORMATION_SCHEMA.QUERY_HISTORY()` table function.

## Configure Snowflake

Snowtop uses the [Snowflake Python Connector's connection configuration](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-connect).
Create `~/.snowflake/connections.toml` with a named profile:

```toml
[snowtop]
account = "myorg-myaccount"
user = "me@example.com"
authenticator = "externalbrowser" # opens your SSO browser
role = "SNOWTOP_MONITOR"           # optional; use your normal role if omitted
database = "ANALYTICS"             # any database this role can use
```

For a default profile, create `~/.snowflake/config.toml`:

```toml
default_connection_name = "snowtop"
```

Then run `uv run snowtop`, or choose a profile explicitly:

```bash
uv run snowtop --connection snowtop
```

Browser SSO is the intended interactive setup. Snowtop never needs a warehouse because it reads
Snowflake metadata. Keep these files private, especially if a profile uses a password or token:

```bash
chmod 700 ~/.snowflake
chmod 600 ~/.snowflake/connections.toml ~/.snowflake/config.toml
```

By default, the Snowflake connector caches the browser SSO/MFA credential in your OS keychain.
Use `--no-credential-cache` on shared machines if you prefer to sign in every time.

Snowtop shows your own query history with an ordinary role. To see other users' queries, grant
the selected role `MONITOR` or `OPERATE` on the relevant warehouses; `ACCOUNTADMIN` is not
needed. Snowflake's [QUERY_HISTORY privileges](https://docs.snowflake.com/en/sql-reference/functions/query_history)
determine exactly what is visible.

```sql
GRANT USAGE ON DATABASE ANALYTICS TO ROLE SNOWTOP_MONITOR;
GRANT MONITOR ON WAREHOUSE REPORTING_WH TO ROLE SNOWTOP_MONITOR;
GRANT ROLE SNOWTOP_MONITOR TO USER your_user;
```

At startup Snowtop connects with this profile, selects its configured database (or another
database the role can use), applies a unique query tag to its private session, and queries
`INFORMATION_SCHEMA.QUERY_HISTORY()`. That history is limited to the previous seven days and
contains only the queries the active role can view.

Query text, error messages, query tags, and user names can be sensitive. Run Snowtop only in a
trusted terminal and use a role scoped to the query history you are allowed to inspect. Snowtop
removes terminal control codes from Snowflake-provided text before displaying it.

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
| `c` | copy the SQL currently shown in the detail pane to the system clipboard |
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

Scope follows your **role's privileges**. By default, you see your own queries. A role with
`MONITOR` or `OPERATE` on a warehouse can also see queries that ran there. Override the profile's
role for one invocation with `-r/--role`:

```bash
uv run snowtop -r SNOWTOP_MONITOR
```

## All options

```
-c, --connection   named connection from connections.toml (default: your default)
-r, --role         override role (use MONITOR/OPERATE on relevant warehouses to see other users)
    --history      start in history mode instead of live
    --since DUR    history window: 30m / 4h / 1d (default 1d)
    --limit N      max rows to fetch (default 1000)
-n, --interval     live auto-refresh seconds (default 3; 0 disables)
    --warn N       flag queries running longer than N seconds (default 300)
    --no-credential-cache
                 do not persist the SSO/MFA credential in the OS keychain
    --show-snowtop-queries
                 include Snowtop's own metadata queries
    --once         print one snapshot and exit (no TUI)
    --demo         synthetic data, no Snowflake connection (try the UI)
```

## Notes

- First run opens the browser once for SSO; `keyring` then caches the token so later runs
  reuse it.
- Snowtop assigns a unique query tag to its private session and hides those metadata queries by
  default. Pass `--show-snowtop-queries` to include them.
