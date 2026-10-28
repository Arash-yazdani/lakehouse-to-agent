import json
from datetime import date

import pyarrow.compute  # noqa: F401
import pytest

import ingest
import lake
import server
from mcp.server.mcpserver.exceptions import ToolError


@pytest.fixture
def wh(tmp_path):
    """A tiny lake: 2 categories x 14 months, loaded as history + latest-2-months snapshots."""
    months = [date(2025, m, 1) for m in range(1, 13)] + [date(2026, 1, 1), date(2026, 2, 1)]
    series = {"RSNSR": [(m, 100 + i) for i, m in enumerate(months)],
              "RSDBS": [(m, 200) for m in months]}
    ingest.load(ingest.to_table(series), tmp_path)
    return tmp_path


def test_two_snapshots_and_time_travel(wh):
    snaps = lake.snapshots(warehouse=wh)
    assert [s["added_records"] for s in snaps] == ["24", "4"]
    old = lake.query("select count(*), max(month) from monthly_sales", snaps[0]["snapshot_id"], warehouse=wh)
    new = lake.query("select count(*), max(month) from monthly_sales", warehouse=wh)
    assert old["rows"] == [[24, "2025-12-01"]]
    assert new["rows"] == [[28, "2026-02-01"]]


def test_yoy_query(wh):
    sql = open("queries.sql").read().split(";")[0]
    r = lake.query(sql, warehouse=wh)
    by = {row[0]: row[3] for row in r["rows"]}
    assert by["Food and beverage stores"] == 0.0
    assert by["Nonstore retailers"] == round(100 * 12 / 101, 1)


@pytest.mark.parametrize("sql", ["drop table monthly_sales", "select 1; drop table x",
                                 "copy monthly_sales to 'x.csv'", "attach 'x.db'", "pragma version",
                                 "install httpfs"])
def test_writes_rejected(wh, sql):
    with pytest.raises(lake.QueryRejected):
        lake.query(sql, warehouse=wh)


def test_tokens_hashed_revocable_and_fail_closed(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"acme": server._hash("t-acme"), "beta": server._hash("t-beta")}))
    assert server.caller_for("t-acme", path) == "acme"
    assert "t-acme" not in path.read_text()            # only hashes at rest
    for bad in (None, "", "nope"):
        with pytest.raises(PermissionError):
            server.caller_for(bad, path)
    path.write_text(json.dumps({"beta": server._hash("t-beta")}))  # revoke acme
    with pytest.raises(PermissionError):
        server.caller_for("t-acme", path)
    assert server.caller_for("t-beta", path) == "beta"


def test_audit_log(tmp_path):
    p = tmp_path / "audit.jsonl"
    server.audit("acme", "query", {"sql": "select 1", "rows": 1}, p)
    rec = json.loads(p.read_text())
    assert rec["caller"] == "acme" and rec["sql"] == "select 1"


def test_snapshot_ids_cross_json_as_strings(wh, monkeypatch, tmp_path):
    # Iceberg ids exceed 2^53: as JSON numbers they get rounded and time travel misses.
    big = lake.snapshots(warehouse=wh)[0]["snapshot_id"]
    assert int(float(big)) != big or big < 2**53
    monkeypatch.setattr(lake, "WAREHOUSE", wh)
    monkeypatch.setenv("LAKE_TOKEN", "t")
    monkeypatch.setattr(server, "TOKENS", tmp_path / "tokens.json")
    monkeypatch.setattr(server, "AUDIT", tmp_path / "audit.jsonl")
    (tmp_path / "tokens.json").write_text(json.dumps({"x": server._hash("t")}))
    real = lake.query
    monkeypatch.setattr(lake, "query", lambda sql, snapshot_id=None, **k: real(sql, snapshot_id, warehouse=wh))
    r = server.query("select count(*) from monthly_sales", snapshot_id=str(big))
    assert r["rows"] == [[24]] and r["snapshot_id"] == str(big)


def test_rejection_reason_reaches_the_agent(monkeypatch, tmp_path):
    # The MCP SDK hides the message of any exception that isn't a ToolError: the agent saw only
    # "Error executing tool query". The guard's reason has to come through, and still be audited.
    monkeypatch.setenv("LAKE_TOKEN", "t")
    monkeypatch.setattr(server, "TOKENS", tmp_path / "tokens.json")
    monkeypatch.setattr(server, "AUDIT", tmp_path / "audit.jsonl")
    (tmp_path / "tokens.json").write_text(json.dumps({"x": server._hash("t")}))
    with pytest.raises(ToolError, match="read-only"):
        server.query("drop table monthly_sales")
    assert json.loads((tmp_path / "audit.jsonl").read_text())["rejected"].startswith("read-only")
