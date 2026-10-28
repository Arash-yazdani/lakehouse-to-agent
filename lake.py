"""Query layer: Iceberg table -> Arrow -> DuckDB, read-only, optional time travel by snapshot."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import duckdb
from pyiceberg.catalog.sql import SqlCatalog

WAREHOUSE = Path(__file__).parent / "warehouse"
TABLES = {"monthly_sales": "retail.monthly_sales"}

# One statement, read-only. ponytail: allowlist by first keyword plus a deny-list of writers;
# the connection is also in-memory over Arrow, so even a missed write cannot touch the lake.
_READ = re.compile(r"^\s*(select|with|describe|summarize)\b", re.I)
_WRITE = re.compile(r"\b(insert|update|delete|drop|create|alter|copy|attach|install|load|pragma|export|call)\b", re.I)


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

_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


def check_sql(sql: str) -> str:
    s = _COMMENT.sub(" ", sql).strip().rstrip(";")
    if ";" in s:
        raise QueryRejected("one statement only")
    if not _READ.match(s) or _WRITE.search(s):
        raise QueryRejected("read-only: SELECT / WITH / DESCRIBE / SUMMARIZE only")
    return s


def connect(snapshot_id: Optional[int] = None, warehouse: Path = WAREHOUSE) -> duckdb.DuckDBPyConnection:
    # No file access from SQL: the guard allows SELECT, and read_json / read_text / read_csv would
    # otherwise let any caller read the audit log, the token hashes or any file the server can open.
    con = duckdb.connect(config={"enable_external_access": False, "autoload_known_extensions": False,
                                 "autoinstall_known_extensions": False, "lock_configuration": True})
    cat = catalog(warehouse)
    for view, ident in TABLES.items():
        con.register(view, cat.load_table(ident).scan(snapshot_id=snapshot_id).to_arrow())
    return con


def query(sql: str, snapshot_id: Optional[int] = None, limit: int = 500,
          warehouse: Path = WAREHOUSE) -> dict:
    s = check_sql(sql)
    cur = connect(snapshot_id, warehouse).execute(s)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchmany(limit)
    return {"columns": cols, "rows": [[str(v) if hasattr(v, "isoformat") else v for v in r] for r in rows],
            "row_count": len(rows), "truncated": len(rows) == limit, "snapshot_id": snapshot_id}
