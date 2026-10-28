"""MCP server over the Iceberg lake: read-only SQL with per-caller tokens and an audit log.

Identity-bound access, the way a customer integration should work: each caller gets its own
token (stored hashed in tokens.json), every query is written to audit.jsonl with the caller's
name, and a token can be revoked without touching anyone else.

    python server.py add-token <caller>      # prints a new token once; stores only its hash
    python server.py revoke <caller>
    LAKE_TOKEN=<token> python server.py      # serve over stdio (Claude Code / Desktop)
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer

import lake

ROOT = Path(__file__).parent
TOKENS = ROOT / "tokens.json"
AUDIT = ROOT / "audit.jsonl"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load_tokens(path: Path = TOKENS) -> dict[str, str]:
    return json.loads(path.read_text()) if path.exists() else {}


def caller_for(token: Optional[str], path: Path = TOKENS) -> str:
    """Caller name for a token, or PermissionError. Fails closed on a missing token."""
    if not token:
        raise PermissionError("no LAKE_TOKEN")
    by_hash = {h: c for c, h in _load_tokens(path).items()}
    caller = by_hash.get(_hash(token))
    if caller is None:
        raise PermissionError("unknown or revoked token")
    return caller


def audit(caller: str, tool: str, detail: dict, path: Path = AUDIT) -> None:
    with path.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "caller": caller, "tool": tool, **detail}) + "\n")


mcp = MCPServer("lakehouse")


@mcp.tool()
def list_tables() -> dict:
    """Tables, their columns and Iceberg snapshots. Call this first."""
    caller = caller_for(os.environ.get("LAKE_TOKEN"))
    audit(caller, "list_tables", {})
    out = {}
    for view in lake.TABLES:
        cols = lake.query(f"DESCRIBE {view}")["rows"]
        out[view] = {"columns": [{"name": r[0], "type": r[1]} for r in cols],
                     "snapshots": lake.snapshots(view),
                     "notes": "Census retail sales by kind of business, $ millions, monthly, via FRED."}
    return out


@mcp.tool()
def query(sql: str, snapshot_id: Optional[int] = None) -> dict:
    """Run ONE read-only SQL statement (DuckDB dialect) against the lake.

    Pass snapshot_id from list_tables to query the table as of that snapshot (time travel).
    Cite the returned rows in any answer."""
    caller = caller_for(os.environ.get("LAKE_TOKEN"))
    try:
        result = lake.query(sql, snapshot_id=snapshot_id)
    except lake.QueryRejected as exc:
        audit(caller, "query", {"sql": sql, "rejected": str(exc)})
        raise
    audit(caller, "query", {"sql": sql, "snapshot_id": snapshot_id, "rows": result["row_count"]})
    return result


def main(argv: list[str]) -> None:
    if argv[:1] == ["add-token"] and len(argv) == 2:
        tokens, token = _load_tokens(), secrets.token_urlsafe(32)
        tokens[argv[1]] = _hash(token)
        TOKENS.write_text(json.dumps(tokens, indent=2))
        print(token)
    elif argv[:1] == ["revoke"] and len(argv) == 2:
        tokens = _load_tokens()
        tokens.pop(argv[1], None)
        TOKENS.write_text(json.dumps(tokens, indent=2))
    elif not argv:
        caller_for(os.environ.get("LAKE_TOKEN"))  # refuse to start with a bad token
        mcp.run()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
