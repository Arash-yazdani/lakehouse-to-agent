import json
from datetime import date

import pyarrow.compute  # noqa: F401
import pytest

import ingest
import lake
import server


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
    assert by["Department stores"] == 0.0
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
