"""snowtop — watch Snowflake queries from the terminal.

An interactive TUI (like Snowsight's Query History, in your terminal) plus a
scriptable one-shot snapshot mode.

Data source: the INFORMATION_SCHEMA.QUERY_HISTORY() table function, which runs in
Snowflake's cloud-services layer and consumes NO warehouse compute credits — so
both the live watch and the history browser are effectively free.

  live mode     RUNNING / QUEUED queries right now, auto-refreshing
  history mode  every query in a time window, all statuses (like Snowsight)

Scope is whatever the connection's role can see: a role with account-wide MONITOR
sees every user's queries; otherwise you see only your own.

Strictly read-only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_COLUMNS = (
    "query_id", "user_name", "role_name", "warehouse_name", "warehouse_size",
    "warehouse_type", "cluster_number", "execution_status", "query_type",
    "database_name", "schema_name", "query_tag", "error_code", "error_message",
    "start_time", "end_time",
    "queued_overload_time", "queued_provisioning_time", "queued_repair_time",
    "transaction_blocked_time",
    "bytes_scanned", "partitions_scanned", "partitions_total",
    "bytes_spilled_to_local_storage", "bytes_spilled_to_remote_storage",
    "query_text",
)

_RUNNING_STATUSES = "('RUNNING', 'QUEUED', 'BLOCKED', 'RESUMING_WAREHOUSE')"


def build_query(
    mode: str,
    since_min: int,
    limit: int,
    available_columns: "set[str] | None" = None,
    exclude_query_tag: "str | None" = None,
) -> str:
    """SQL for the info-schema QUERY_HISTORY table function.

    duration_ms is computed against CURRENT_TIMESTAMP for in-flight queries so it
    stays live, and against end_time for finished ones.
    """
    limit = int(limit)
    columns = _COLUMNS
    if available_columns is not None:
        columns = tuple(c for c in columns if c.upper() in available_columns)
    column_sql = ",\n       ".join(columns)
    own_query_filter = ""
    if exclude_query_tag:
        own_query_filter = f"\n  AND COALESCE(query_tag, '') <> '{exclude_query_tag}'"
    if mode == "live":
        return f"""
SELECT {column_sql},
       TIMESTAMPDIFF(millisecond, start_time, CURRENT_TIMESTAMP()) AS duration_ms
FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY(RESULT_LIMIT => {limit}))
WHERE execution_status IN {_RUNNING_STATUSES}
{own_query_filter}
ORDER BY duration_ms DESC
"""
    since_min = int(since_min)
    return f"""
SELECT {column_sql},
       TIMESTAMPDIFF(millisecond, start_time, COALESCE(end_time, CURRENT_TIMESTAMP())) AS duration_ms
FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY(RESULT_LIMIT => {limit}))
WHERE start_time >= DATEADD('minute', -{since_min}, CURRENT_TIMESTAMP())
{own_query_filter}
ORDER BY start_time DESC
"""


# ---------------------------------------------------------------------------
# Parsing / formatting helpers
# ---------------------------------------------------------------------------

_TARGET_RE = re.compile(
    r"""
    \b(?:
        CREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+|TEMP(?:ORARY)?\s+|VOLATILE\s+)*
            (?:TABLE|VIEW|MATERIALIZED\s+VIEW|DYNAMIC\s+TABLE)\s+(?:IF\s+NOT\s+EXISTS\s+)?
      | INSERT\s+(?:OVERWRITE\s+)?INTO\s+
      | MERGE\s+INTO\s+
      | COPY\s+INTO\s+
      | UPDATE\s+
      | DELETE\s+FROM\s+
      | TRUNCATE\s+(?:TABLE\s+)?
    )
    (?P<name>"[^"]+"(?:\.\s*"[^"]+")*|[A-Za-z_][\w$]*(?:\s*\.\s*[A-Za-z_$"][\w$]*)*)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_DBT_COMMENT_RE = re.compile(r"/\*\s*(\{.*?\})\s*\*/", re.DOTALL)


def target_object(query_text: str, query_type: str) -> str:
    if not query_text:
        return (query_type or "").replace("_", " ").title()
    m = _TARGET_RE.search(query_text)
    if m:
        return re.sub(r"\s+", "", m.group("name"))
    return (query_type or "").replace("_", " ").title()


def dbt_info(query_text: str) -> dict:
    """Pull dbt node info from the leading /* {...} */ comment dbt injects."""
    if not query_text:
        return {}
    m = _DBT_COMMENT_RE.search(query_text[:2000])
    if not m:
        return {}
    try:
        meta = json.loads(m.group(1))
    except (ValueError, TypeError):
        return {}
    if not isinstance(meta, dict) or meta.get("app") != "dbt":
        return {}
    node = meta.get("node_id") or ""
    parts = node.split(".")
    return {
        "resource": parts[0] if len(parts) > 1 else "",  # model / test / snapshot / seed
        "name": parts[-1] if node else "",
        "env": meta.get("target_name") or "",
        "version": meta.get("dbt_version") or "",
        "invocation_id": meta.get("invocation_id") or "",
    }


def dbt_label(query_text: str) -> str:
    info = dbt_info(query_text)
    if not info or not info.get("name"):
        return ""
    res = info.get("resource") or "node"
    env = info.get("env")
    return f"{res}:{info['name']}" + (f" ({env})" if env else "")


def humanize_ms(ms) -> str:
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        return "-"
    if ms < 0:
        ms = 0
    if ms < 1000:
        return f"{ms}ms"
    s = ms / 1000.0
    if s < 10:
        return f"{s:.1f}s"
    if s < 60:
        return f"{int(s)}s"
    m, sec = divmod(int(s), 60)
    if m < 60:
        return f"{m}m {sec}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def fmt_time(dt) -> str:
    try:
        return dt.strftime("%m-%d %H:%M:%S")
    except (AttributeError, ValueError):
        return "-"


def oneline(text: str, limit: int = 300) -> str:
    return " ".join((text or "").split())[:limit] or "-"


def queued_ms(r: dict) -> int:
    total = 0
    for k in ("queued_overload_time", "queued_provisioning_time", "queued_repair_time"):
        try:
            total += int(r.get(k) or 0)
        except (TypeError, ValueError):
            pass
    return total


def spilled(r: dict) -> bool:
    for k in ("bytes_spilled_to_local_storage", "bytes_spilled_to_remote_storage"):
        try:
            if int(r.get(k) or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return False


# ---------------------------------------------------------------------------
# Snowflake connection
# ---------------------------------------------------------------------------

def default_connection_name() -> "str | None":
    import os

    env = os.environ.get("SNOWFLAKE_DEFAULT_CONNECTION_NAME")
    if env:
        return env
    try:
        import tomllib
        from pathlib import Path

        cfg = Path.home() / ".snowflake" / "config.toml"
        if cfg.exists():
            data = tomllib.loads(cfg.read_text())
            name = data.get("default_connection_name")
            if name:
                return name
    except Exception:  # noqa: BLE001
        pass
    return None


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def connect(connection_name: "str | None", role: "str | None"):
    import snowflake.connector

    name = connection_name or default_connection_name()
    kwargs = {"client_store_temporary_credential": True}
    if name:
        kwargs["connection_name"] = name
    if role:
        kwargs["role"] = role
    conn = snowflake.connector.connect(**kwargs)
    _ensure_database(conn)
    return conn


def _ensure_database(conn) -> None:
    """QUERY_HISTORY() resolves against the current database, so we need one
    selected. It's account-scoped, so any accessible database works."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT CURRENT_DATABASE()")
        if cur.fetchone()[0]:
            return
        candidates = []
        if getattr(conn, "database", None):
            candidates.append(conn.database)
        cur.execute("SHOW DATABASES")
        candidates += [row[1] for row in cur.fetchall()]
        for db in candidates:
            try:
                cur.execute(f"USE DATABASE {_quote_ident(db)}")
                return
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError(
            "No accessible database to anchor INFORMATION_SCHEMA queries. "
            "Grant the role USAGE on a database."
        )
    finally:
        cur.close()


def _rows_from_cursor(cur) -> "list[dict]":
    cols = [c[0].lower() for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _query_history_columns(conn) -> set[str]:
    """Return the QUERY_HISTORY columns supported by this Snowflake account.

    Snowflake rolls new QUERY_HISTORY columns out through behavior-change
    bundles. Selecting a newer optional column against an account where its
    bundle is not enabled raises an invalid-identifier compilation error.
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY(RESULT_LIMIT => 1))")
        return {column[0].upper() for column in cur.description}
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

class SnowflakeSource:
    """Live source backed by a persistent connection (one login, reused)."""

    def __init__(self, connection_name, role, show_snowtop_queries=False):
        self.conn = connect(connection_name, role)
        self.query_tag = None
        if not show_snowtop_queries:
            self.query_tag = f"snowtop:{uuid.uuid4().hex}"
            cur = self.conn.cursor()
            try:
                cur.execute(f"ALTER SESSION SET QUERY_TAG = '{self.query_tag}'")
            finally:
                cur.close()
        self.query_history_columns = _query_history_columns(self.conn)
        cur = self.conn.cursor()
        try:
            cur.execute("SELECT CURRENT_ROLE(), CURRENT_ACCOUNT()")
            self.role, self.account = cur.fetchone()
        finally:
            cur.close()

    def fetch(self, mode, since_min, limit):
        cur = self.conn.cursor()
        try:
            cur.execute(build_query(
                mode, since_min, limit, self.query_history_columns, self.query_tag
            ))
            return _rows_from_cursor(cur)
        finally:
            cur.close()

    def close(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


class DemoSource:
    """Synthetic data so the UI runs (and is testable) without Snowflake."""

    role = "DEMO_ROLE"
    account = "DEMO"

    _SQL = [
        ('/* {"app":"dbt","dbt_version":"1.7.9","target_name":"prod",'
         '"node_id":"model.groupone.fct_orders"} */\n'
         "create or replace transient table GROUPONEDATA_PROD.MART.fct_orders as\n"
         "select o.*, c.segment from staging.orders o join dim_customer c using (customer_id)",
         "CREATE_TABLE_AS_SELECT", "TRANSFORMER_WH", "Large", "DBT_SVC"),
        ("SELECT query_id, user_name, warehouse_name FROM TABLE(INFORMATION_SCHEMA."
         "QUERY_HISTORY()) WHERE execution_status = 'RUNNING'",
         "SELECT", "COMPUTE_WH", "X-Small", "EMIL.NILSSON@GROUP.ONE"),
        ("create or replace temporary table SB_EMIL_SANDBOX.MART.tmp_scratch as select 1",
         "CREATE_TABLE_AS_SELECT", "ML_WH", "Large", "EMIL.NILSSON@GROUP.ONE"),
        ("merge into MART.dim_customer t using stg_customer s on t.id = s.id "
         "when matched then update set t.name = s.name",
         "MERGE", "TRANSFORMER_WH", "Large", "DBT_SVC"),
        ("copy into RAW.PUBLIC.landing from @my_stage file_format = (type = parquet)",
         "COPY", "DATA_INTEGRATION_WH", "X-Small", "SYSTEM"),
    ]
    _STATUSES = ["RUNNING", "SUCCESS", "RUNNING", "QUEUED", "FAILED"]
    _DUR = [42000, 340, 3400, 0, 128000]

    def fetch(self, mode, since_min, limit):
        now = datetime.now(timezone.utc)
        rows = []
        for i, (sql, qtype, wh, size, user) in enumerate(self._SQL):
            status = self._STATUSES[i]
            dur = self._DUR[i]
            if mode == "live" and status not in ("RUNNING", "QUEUED"):
                continue
            start = now - timedelta(milliseconds=dur if status in ("RUNNING", "QUEUED") else dur + i * 60000)
            rows.append({
                "query_id": f"01demo-{i:04d}",
                "user_name": user,
                "role_name": "DBT_SVC_ROLE" if user == "DBT_SVC" else "ANALYST",
                "warehouse_name": wh,
                "warehouse_size": size,
                "warehouse_type": "STANDARD",
                "cluster_number": 1,
                "execution_status": status,
                "query_type": qtype,
                "database_name": "GROUPONEDATA_PROD",
                "schema_name": "MART",
                "query_tag": "dbt:run" if user == "DBT_SVC" else "",
                "error_code": "000904" if status == "FAILED" else None,
                "error_message": "SQL compilation error: invalid identifier" if status == "FAILED" else None,
                "start_time": start,
                "end_time": None if status in ("RUNNING", "QUEUED") else now,
                "queued_overload_time": 1500 if status == "QUEUED" else 0,
                "queued_provisioning_time": 0,
                "queued_repair_time": 0,
                "transaction_blocked_time": 0,
                "bytes_scanned": dur * 1000,
                "partitions_scanned": i * 10,
                "partitions_total": 100,
                "bytes_spilled_to_local_storage": 500_000 if i == 4 else 0,
                "bytes_spilled_to_remote_storage": 0,
                "query_text": sql,
                "duration_ms": dur,
            })
        return rows

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Shared rendering bits (used by both TUI and --once)
# ---------------------------------------------------------------------------

def status_style(r: dict, warn_s: int) -> str:
    status = (r.get("execution_status") or "").upper()
    if status in ("FAILED", "FAILED_WITH_ERROR", "INCIDENT"):
        return "red"
    if status == "QUEUED" or queued_ms(r) > 0:
        return "yellow"
    if status == "RUNNING":
        try:
            if int(r.get("duration_ms") or 0) >= warn_s * 1000:
                return "bold red"
        except (TypeError, ValueError):
            pass
        return "cyan"
    if status == "SUCCESS":
        return "green"
    return "white"


def duration_cell(r: dict, max_ms: int, warn_s: int, width: int = 16):
    from rich.text import Text

    try:
        ms = int(r.get("duration_ms") or 0)
    except (TypeError, ValueError):
        ms = 0
    frac = 0.0 if max_ms <= 0 else min(1.0, ms / max_ms)
    filled = int(round(frac * width))
    style = status_style(r, warn_s)
    t = Text()
    t.append("█" * filled, style=style)
    t.append("·" * (width - filled), style="grey37")
    t.append(" " + humanize_ms(ms))
    if spilled(r):
        t.append(" ⚠", style="red")
    return t


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

def run_tui(args) -> int:
    from rich.console import Group
    from rich.syntax import Syntax
    from rich.text import Text
    from textual import work
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.widgets import DataTable, Footer, Input, Static

    # (column key, header label, sort field or None)
    COLUMNS = [
        ("idx", "#", None),
        ("sql", "SQL TEXT", "query_text"),
        ("status", "STATUS", "execution_status"),
        ("user", "USER", "user_name"),
        ("role", "ROLE", "role_name"),
        ("wh", "WAREHOUSE", "warehouse_name"),
        ("size", "SIZE", "warehouse_size"),
        ("dur", "DURATION", "duration_ms"),
        ("started", "STARTED", "start_time"),
    ]
    STATUS_CYCLE = [None, "RUNNING", "QUEUED", "SUCCESS", "FAILED"]

    class SnowtopApp(App):
        CSS = """
        #statusbar { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
        #filter { height: 3; border: tall $accent; }
        #table { height: 2fr; }
        #detail { height: 1fr; border-top: solid $accent; padding: 0 1; }
        """
        BINDINGS = [
            ("q", "quit", "Quit"),
            ("r", "reload", "Reload"),
            ("enter", "toggle_detail_lock", "Lock view"),
            ("h", "toggle_mode", "Live/History"),
            ("s", "cycle_status", "Status"),
            ("f", "focus_filter", "Filter"),
            ("d", "sort('duration_ms')", "Sort dur"),
            ("t", "sort('start_time')", "Sort time"),
            ("u", "sort('user_name')", "Sort user"),
        ]

        def __init__(self, args):
            super().__init__()
            self.args = args
            self.mode = args.mode
            self.since_min = args.since_min
            self.limit = args.limit
            self.warn_s = args.warn
            self.source = None
            self.role = "?"
            self.all_rows = []
            self.row_index = {}
            self.status_filter = None
            self.text_filter = ""
            self.sort_field = "duration_ms" if self.mode == "live" else "start_time"
            self.sort_reverse = True
            self._timer = None
            self._detail_shown = False
            self.selected_query_id = None
            self.locked_row = None

        def compose(self) -> ComposeResult:
            yield Static(id="statusbar")
            yield Input(placeholder="filter by user or SQL text…", id="filter")
            yield DataTable(id="table", zebra_stripes=True, cursor_type="row")
            with VerticalScroll(id="detail"):
                yield Static(id="sql")
            yield Footer()

        def on_mount(self):
            table = self.query_one(DataTable)
            for key, label, _ in COLUMNS:
                w = 44 if key == "sql" else (20 if key == "dur" else None)
                table.add_column(label, key=key, width=w)
            table.focus()  # so arrow keys + shortcuts work immediately (not the filter box)
            if getattr(self.args, "demo", False):
                self.source = DemoSource()
                self.role = self.source.role
                self._after_connect()
            else:
                self._set_status("[yellow]Connecting…[/] a browser window may open for SSO login")
                self._connect_worker()

        # -- connection / loading (blocking work off the UI thread) --
        @work(thread=True, exclusive=True, group="connect")
        def _connect_worker(self):
            try:
                src = SnowflakeSource(
                    self.args.connection, self.args.role,
                    getattr(self.args, "show_snowtop_queries", False),
                )
            except Exception as e:  # noqa: BLE001
                self.call_from_thread(self._set_status, f"[red]Connect failed:[/] {e}")
                return
            self.call_from_thread(self._connected, src)

        def _connected(self, src):
            self.source = src
            self.role = src.role
            self._after_connect()

        def _after_connect(self):
            self.action_reload()
            self._restart_timer()

        def _restart_timer(self):
            if self._timer is not None:
                self._timer.stop()
                self._timer = None
            if self.mode == "live" and self.args.interval > 0:
                self._timer = self.set_interval(self.args.interval, self.action_reload)

        @work(thread=True, exclusive=True, group="load")
        def _load_worker(self):
            try:
                rows = self.source.fetch(self.mode, self.since_min, self.limit)
            except Exception as e:  # noqa: BLE001
                self.call_from_thread(self._set_status, f"[red]Query error:[/] {e}")
                return
            self.call_from_thread(self._got_rows, rows)

        def _got_rows(self, rows):
            self.all_rows = rows
            self._repopulate()

        # -- view / filtering / sorting --
        def _view_rows(self):
            rows = self.all_rows
            if self.status_filter:
                sf = self.status_filter.upper()
                rows = [r for r in rows if (r.get("execution_status") or "").upper() == sf]
            if self.text_filter:
                tf = self.text_filter
                rows = [
                    r for r in rows
                    if tf in (r.get("user_name") or "").lower()
                    or tf in (r.get("query_text") or "").lower()
                ]

            def keyf(r):
                v = r.get(self.sort_field)
                if self.sort_field == "duration_ms":
                    try:
                        return float(v or 0)
                    except (TypeError, ValueError):
                        return 0.0
                if self.sort_field == "start_time":
                    try:
                        return v.timestamp()
                    except (AttributeError, ValueError):
                        return 0.0
                return str(v or "").lower()

            return sorted(rows, key=keyf, reverse=self.sort_reverse)

        def _repopulate(self):
            table = self.query_one(DataTable)
            table.clear()
            rows = self._view_rows()
            max_ms = max((int(r.get("duration_ms") or 0) for r in rows), default=0)
            self.row_index = {}
            for i, r in enumerate(rows, 1):
                key = r.get("query_id") or f"row{i}"
                self.row_index[key] = r
                table.add_row(
                    str(i),
                    Text(oneline(r.get("query_text"), 200), no_wrap=True, overflow="ellipsis"),
                    Text((r.get("execution_status") or "-").title(), style=status_style(r, self.warn_s)),
                    r.get("user_name") or "-",
                    r.get("role_name") or "-",
                    r.get("warehouse_name") or "-",
                    r.get("warehouse_size") or "-",
                    duration_cell(r, max_ms, self.warn_s),
                    fmt_time(r.get("start_time")),
                    key=key,
                )
            self._update_statusbar(len(rows))
            if rows and not self._detail_shown:
                self.selected_query_id = rows[0].get("query_id")
                self._show_detail(rows[0])
                self._detail_shown = True

        def _update_statusbar(self, shown):
            arrow = "▼" if self.sort_reverse else "▲"
            since = _fmt_since(self.since_min) if self.mode == "history" else ""
            now = datetime.now().strftime("%H:%M:%S")
            parts = [
                f"[b]{self.mode.upper()}[/b]",
                f"conn={self.args.connection or 'default'}",
                f"role={self.role}",
                f"status={self.status_filter or 'All'}",
            ]
            if self.locked_row:
                parts.append("[yellow]DETAIL LOCKED[/]")
            if since:
                parts.append(f"since={since}")
            parts.append(f"sort={self.sort_field}{arrow}")
            parts.append(f"[b]{shown}[/b]/{len(self.all_rows)} shown")
            parts.append(f"updated {now}")
            self._set_status("  ·  ".join(parts))

        def _set_status(self, text):
            self.query_one("#statusbar", Static).update(text)

        def _show_detail(self, r):
            if not r:
                return
            meta = Text()

            def line(label, value, style=None):
                if value in (None, "", "-"):
                    return
                meta.append(f"{label}: ", style="bold")
                meta.append(str(value) + "\n", style=style)

            line("Query ID", r.get("query_id"))
            status = (r.get("execution_status") or "-")
            line("Status", status.title(), status_style(r, self.warn_s))
            if r.get("error_code") or r.get("error_message"):
                line("Error", f"{r.get('error_code') or ''} {r.get('error_message') or ''}".strip(), "red")
            line("Target", target_object(r.get("query_text") or "", r.get("query_type") or ""))
            dbt = dbt_label(r.get("query_text") or "")
            if dbt:
                line("dbt", dbt, "magenta")
            line("User / Role", f"{r.get('user_name') or '-'}  /  {r.get('role_name') or '-'}")
            wh = r.get("warehouse_name") or "-"
            if r.get("warehouse_size"):
                wh += f" ({r.get('warehouse_size')}"
                if r.get("cluster_number"):
                    wh += f", cluster {r.get('cluster_number')}"
                wh += ")"
            line("Warehouse", wh)
            line("Database", f"{r.get('database_name') or '-'}.{r.get('schema_name') or '-'}")
            line("Duration", humanize_ms(r.get("duration_ms")))
            if queued_ms(r):
                line("Queued", humanize_ms(queued_ms(r)), "yellow")
            if r.get("transaction_blocked_time"):
                line("Blocked on lock", humanize_ms(r.get("transaction_blocked_time")), "yellow")
            if spilled(r):
                line("⚠ Spilling", "warehouse may be undersized", "red")
            pt, ps = r.get("partitions_total"), r.get("partitions_scanned")
            if pt:
                line("Partitions", f"{ps or 0}/{pt}")
            line("Query tag", r.get("query_tag"))
            line("Started / Ended", f"{fmt_time(r.get('start_time'))}  →  {fmt_time(r.get('end_time')) if r.get('end_time') else '(running)'}")

            sql = r.get("query_text") or "(no SQL text available for this query)"
            syntax = Syntax(sql, "sql", theme="ansi_dark", word_wrap=True, background_color="default")
            self.query_one("#sql", Static).update(Group(meta, Text("─" * 40, style="grey37"), syntax))

        # -- events --
        def on_data_table_row_highlighted(self, event):
            r = self.row_index.get(getattr(event.row_key, "value", None))
            if r:
                self.selected_query_id = r.get("query_id")
                if self.locked_row is None:
                    self._show_detail(r)

        def on_data_table_row_selected(self, event):
            self.action_toggle_detail_lock()

        def on_data_table_header_selected(self, event):
            field = {k: f for k, _, f in COLUMNS}.get(getattr(event.column_key, "value", None))
            if field:
                self.action_sort(field)

        def on_input_changed(self, event):
            if event.input.id == "filter":
                self.text_filter = event.value.strip().lower()
                self._repopulate()

        def on_input_submitted(self, event):
            if event.input.id == "filter":
                self.query_one(DataTable).focus()

        # -- actions --
        def action_reload(self):
            if self.source is None:
                return
            self._load_worker()

        def action_toggle_detail_lock(self):
            """Keep the current SQL in the detail pane while live data refreshes."""
            if self.locked_row is not None:
                self.locked_row = None
                r = self.row_index.get(self.selected_query_id)
                if r:
                    self._show_detail(r)
                self.query_one(DataTable).focus()
            else:
                r = self.row_index.get(self.selected_query_id)
                if r is None:
                    return
                self.locked_row = r
                self._show_detail(r)
                self.query_one("#detail", VerticalScroll).focus()
            self._update_statusbar(len(self._view_rows()))

        def action_toggle_mode(self):
            self.mode = "history" if self.mode == "live" else "live"
            self.sort_field = "duration_ms" if self.mode == "live" else "start_time"
            self.sort_reverse = True
            self._restart_timer()
            self.action_reload()

        def action_cycle_status(self):
            i = STATUS_CYCLE.index(self.status_filter) if self.status_filter in STATUS_CYCLE else 0
            self.status_filter = STATUS_CYCLE[(i + 1) % len(STATUS_CYCLE)]
            self._repopulate()

        def action_focus_filter(self):
            self.query_one("#filter", Input).focus()

        def action_sort(self, field):
            if self.sort_field == field:
                self.sort_reverse = not self.sort_reverse
            else:
                self.sort_field = field
                self.sort_reverse = True
            self._repopulate()

        def on_unmount(self):
            if self.source:
                self.source.close()

    app = SnowtopApp(args)
    if getattr(args, "_test_return", False):
        return app  # for headless testing via App.run_test()
    app.run()
    return 0


def _fmt_since(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# One-shot snapshot (scriptable, rich)
# ---------------------------------------------------------------------------

def run_once(args) -> int:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    console = Console()
    try:
        source = DemoSource() if args.demo else SnowflakeSource(
            args.connection, args.role, getattr(args, "show_snowtop_queries", False)
        )
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Failed to connect:[/red] {e}")
        return 2
    try:
        rows = source.fetch(args.mode, args.since_min, args.limit)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Query error:[/red] {e}")
        return 2
    finally:
        source.close()

    max_ms = max((int(r.get("duration_ms") or 0) for r in rows), default=0)
    title = f"Snowflake — {'ongoing queries' if args.mode == 'live' else f'query history (last {_fmt_since(args.since_min)})'}"
    table = Table(title=title, expand=True,
                  caption=f"role={getattr(source, 'role', '?')}  rows={len(rows)}")
    table.add_column("User", style="cyan", no_wrap=True)
    table.add_column("Target", overflow="fold", ratio=2)
    table.add_column("dbt", style="magenta", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Duration", no_wrap=True)
    table.add_column("Warehouse", no_wrap=True)
    for r in rows:
        wh = r.get("warehouse_name") or "-"
        if r.get("warehouse_size"):
            wh += f" ({r.get('warehouse_size')})"
        table.add_row(
            r.get("user_name") or "-",
            target_object(r.get("query_text") or "", r.get("query_type") or "") or "-",
            dbt_label(r.get("query_text") or "") or "-",
            Text((r.get("execution_status") or "-").title(), style=status_style(r, args.warn)),
            duration_cell(r, max_ms, args.warn),
            wh,
        )
    if not rows:
        table.add_row("[dim]no queries[/dim]", "", "", "", "", "")
    console.print(table)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_since(text: str) -> int:
    """'90m' / '4h' / '2d' / '120' (minutes) -> minutes."""
    text = str(text).strip().lower()
    m = re.fullmatch(r"(\d+)\s*([mhd]?)", text)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration: {text!r} (use e.g. 30m, 4h, 1d)")
    n = int(m.group(1))
    return {"": n, "m": n, "h": n * 60, "d": n * 1440}[m.group(2)]


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="snowtop",
        description="Watch Snowflake queries in the terminal (live TUI or one-shot). "
        "Read-only; uses metadata that consumes no warehouse credits.",
    )
    p.add_argument("-c", "--connection", default=None,
                   help="Named connection from connections.toml (default: your default).")
    p.add_argument("-r", "--role", default=None,
                   help="Override the role (use one with account-wide MONITOR to see all users).")
    p.add_argument("--history", action="store_true",
                   help="Start in history mode (all statuses over a time window) instead of live.")
    p.add_argument("--since", type=parse_since, default="1d", metavar="DUR",
                   help="History window, e.g. 30m / 4h / 1d (default: 1d).")
    p.add_argument("--limit", type=int, default=1000,
                   help="Max rows to fetch (default: 1000).")
    p.add_argument("-n", "--interval", type=float, default=3.0,
                   help="Live auto-refresh interval in seconds (default: 3; 0 disables).")
    p.add_argument("--warn", type=int, default=300,
                   help="Flag queries running longer than N seconds (default: 300).")
    p.add_argument("--show-snowtop-queries", action="store_true",
                   help="Include Snowtop's own metadata queries in the results.")
    p.add_argument("--once", action="store_true", help="Print one snapshot and exit (no TUI).")
    p.add_argument("--demo", action="store_true",
                   help="Use synthetic data (no Snowflake connection); for trying the UI.")
    args = p.parse_args(argv)
    args.mode = "history" if args.history else "live"
    args.since_min = args.since if isinstance(args.since, int) else parse_since(args.since)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.demo:
        args.connection = args.connection or default_connection_name()
    try:
        if args.once:
            return run_once(args)
        return run_tui(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
