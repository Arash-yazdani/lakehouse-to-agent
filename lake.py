"""Query layer: Iceberg table -> Arrow -> DuckDB, read-only, optional time travel by snapshot."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import duckdb
from pyiceberg.catalog.sql import SqlCatalog

WAREHOUSE = Path(__file__).parent / "warehouse"
TABLES = {"monthly_sales": "retail.monthly_sales"}
NOTES = {"monthly_sales": "Census retail sales by kind of business, $ millions, monthly, seasonally adjusted, via FRED. "
                          "Grocery stores (RSGCS) are part of Food and beverage stores (RSDBS): never add the two together."}
_BASE = set(TABLES)
# Extra tables (for example licensed data) live in a git-ignored file, so they never reach the public repo.
_LOCAL = Path(__file__).parent / "tables.local.json"
if _LOCAL.exists():
    for _view, _spec in json.loads(_LOCAL.read_text()).items():
        TABLES[_view] = _spec["table"]
        NOTES[_view] = _spec.get("notes", "")

# One statement, read-only: allowlisted first keyword, no writer keywords, comments stripped first.
# The connection is also in-memory over Arrow, so even a missed write cannot touch the lake.
_READ = re.compile(r"^\s*(select|with|describe|summarize)\b", re.I)
_WRITE = re.compile(r"\b(insert|update|delete|drop|create|alter|copy|attach|install|load|pragma|export|call)\b", re.I)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


class QueryRejected(ValueError):
    pass


def catalog(warehouse: Path = WAREHOUSE) -> SqlCatalog:
    return SqlCatalog("local", uri=f"sqlite:///{warehouse}/catalog.db", warehouse=f"file://{warehouse}")


def snapshots(name: str = "monthly_sales", warehouse: Path = WAREHOUSE) -> list[dict]:
    t = catalog(warehouse).load_table(TABLES[name])
    return [{"snapshot_id": s.snapshot_id, "timestamp_ms": s.timestamp_ms,
             "added_records": s.summary.additional_properties.get("added-records")} for s in t.snapshots()]


def current_snapshot_id(name: str = "monthly_sales", warehouse: Path = WAREHOUSE) -> int:
    return catalog(warehouse).load_table(TABLES[name]).current_snapshot().snapshot_id


def check_sql(sql: str) -> str:
    s = _COMMENT.sub(" ", sql).strip().rstrip(";")
    if ";" in s:
        raise QueryRejected("one statement only")
    if not _READ.match(s) or _WRITE.search(s):
        raise QueryRejected("read-only: SELECT / WITH / DESCRIBE / SUMMARIZE only")
    return s


def connect(snapshot_id: Optional[int] = None, warehouse: Path = WAREHOUSE) -> tuple[duckdb.DuckDBPyConnection, dict]:
    # No file access from SQL: the guard allows SELECT, and read_json / read_text / read_csv would
    # otherwise let any caller read the audit log, the token hashes or any file the server can open.
    con = duckdb.connect(config={"enable_external_access": False, "autoload_known_extensions": False,
                                 "autoinstall_known_extensions": False, "lock_configuration": True})
    cat = catalog(warehouse)
    read = {}
    for view, ident in TABLES.items():
        if view not in _BASE and not cat.table_exists(ident):
            continue  # a local extra table that this warehouse (for example a test one) does not hold
        t = cat.load_table(ident)
        # a snapshot id belongs to one table; the others read their current snapshot
        sid = snapshot_id if snapshot_id is not None and t.snapshot_by_id(snapshot_id) else None
        con.register(view, t.scan(snapshot_id=sid).to_arrow())
        snap = t.snapshot_by_id(sid) if sid is not None else t.current_snapshot()
        read[view] = str(snap.snapshot_id) if snap else None  # string: ids exceed JSON's 2^53
    if snapshot_id is not None and str(snapshot_id) not in read.values():
        # a mistyped or JSON-rounded id must fail, not quietly return current data under its label
        raise ValueError(f"snapshot {snapshot_id} not found in any table")
    return con, read


def query(sql: str, snapshot_id: Optional[int] = None, limit: int = 500,
          warehouse: Path = WAREHOUSE) -> dict:
    s = check_sql(sql)
    con, read = connect(snapshot_id, warehouse)
    cur = con.execute(s)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchmany(limit + 1)  # one extra row tells a full result from a cut one
    truncated, rows = len(rows) > limit, rows[:limit]
    return {"columns": cols, "rows": [[str(v) if hasattr(v, "isoformat") else v for v in r] for r in rows],
            "row_count": len(rows), "truncated": truncated, "snapshot_id": snapshot_id,
            # the snapshot each table in the SQL was read at, so every number cites its own table
            "snapshots_read": {v: sid for v, sid in read.items() if re.search(rf"\b{re.escape(v)}\b", s, re.I)}}
