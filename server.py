"""MCP server over the Iceberg lake: read-only SQL with per-caller tokens and an audit log.

Each caller gets its own token, stored as a SHA-256 hash in tokens.json. Every call is appended
to audit.jsonl under the caller's name (a denied token as "unknown"). Revoking one caller leaves
the others working.

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
from mcp.server.mcpserver.exceptions import ToolError

import lake

ROOT = Path(__file__).parent
TOKENS = ROOT / "tokens.json"
AUDIT = ROOT / "audit.jsonl"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load_tokens(path: Optional[Path] = None) -> dict[str, str]:
    path = path or TOKENS
    return json.loads(path.read_text()) if path.exists() else {}


def caller_for(token: Optional[str], path: Optional[Path] = None) -> str:
    """Caller name for a token, or PermissionError. Fails closed on a missing token."""
    if not token:
        raise PermissionError("no LAKE_TOKEN")
    by_hash = {h: c for c, h in _load_tokens(path).items()}
    caller = by_hash.get(_hash(token))
    if caller is None:
        raise PermissionError("unknown or revoked token")
    return caller


def audit(caller: str, tool: str, detail: dict, path: Optional[Path] = None) -> None:
    with (path or AUDIT).open("a") as f:
        f.write(json.dumps({"ts": time.time(), "caller": caller, "tool": tool, **detail}) + "\n")


def _caller(tool: str) -> str:
    """Caller for the request's token; a denied token is audited too, then refused."""
    try:
        return caller_for(os.environ.get("LAKE_TOKEN"))
    except PermissionError as exc:
        audit("unknown", tool, {"denied": str(exc)})
        raise ToolError(str(exc)) from exc


mcp = MCPServer("lakehouse")


@mcp.tool()
def list_tables() -> dict:
    """Tables, their columns and Iceberg snapshots. Call this first."""
    caller = _caller("list_tables")
    audit(caller, "list_tables", {})
    out = {}
    for view in lake.TABLES:
        cols = lake.query(f"DESCRIBE {view}")["rows"]
        out[view] = {"columns": [{"name": r[0], "type": r[1]} for r in cols],
                     # snapshot ids exceed 2^53, so they cross JSON as strings or get rounded
                     "snapshots": [{**x, "snapshot_id": str(x["snapshot_id"])} for x in lake.snapshots(view)],
                     "notes": lake.NOTES.get(view, "")}
    return out


@mcp.tool()
def query(sql: str, snapshot_id: Optional[str] = None) -> dict:
    """Run ONE read-only SQL statement (DuckDB dialect) against the lake.

    Pass snapshot_id (a string, exactly as list_tables returns it) to query the table as of that
    snapshot (time travel). Iceberg ids exceed JSON's exact-integer range, so never send a number.
    Cite the returned rows in any answer."""
    caller = _caller("query")
    try:
        lake.check_sql(sql)
        # every result cites the snapshot it read, default reads included
        sid = int(snapshot_id) if snapshot_id else lake.current_snapshot_id()
        result = lake.query(sql, snapshot_id=sid)
    except lake.QueryRejected as exc:
        audit(caller, "query", {"sql": sql, "rejected": str(exc)})
        # the SDK hides the message of any other exception, so the agent would see only
        # "Error executing tool query"; ToolError carries the reason so it can rewrite the query
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # DuckDB errors, a malformed snapshot id: audited, then reported
        audit(caller, "query", {"sql": sql, "snapshot_id": snapshot_id, "error": str(exc)[:300]})
        raise ToolError(f"query failed: {exc}") from exc
    result["snapshot_id"] = str(sid)  # the snapshot requested; snapshots_read is per table
    audit(caller, "query", {"sql": sql, "snapshot_id": str(sid), "snapshots_read": result["snapshots_read"],
                            "rows": result["row_count"]})
    return result


def main(argv: list[str]) -> None:
    if argv[:1] == ["add-token"] and len(argv) == 2:
        tokens, token = _load_tokens(), secrets.token_urlsafe(32)
        tokens[argv[1]] = _hash(token)
        TOKENS.write_text(json.dumps(tokens, indent=2))
        print(token)
    elif argv[:1] == ["revoke"] and len(argv) == 2:
        tokens = _load_tokens()
        if tokens.pop(argv[1], None) is None:  # a misspelled name must not look like a revocation
            sys.exit(f"no caller named {argv[1]!r}; nothing revoked")
        TOKENS.write_text(json.dumps(tokens, indent=2))
    elif not argv:
        caller_for(os.environ.get("LAKE_TOKEN"))  # refuse to start with a bad token
        mcp.run()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
