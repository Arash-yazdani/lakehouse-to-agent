# lakehouse-to-agent

A customer's lakehouse data, end to end to an AI agent: public data lands as **Parquet**, is registered as an **Apache Iceberg** table, is queried with **DuckDB SQL**, and is served to **Claude over MCP** with per-caller tokens, read-only enforcement and an audit log.

Built in a day as the integration pattern a data provider's solutions team runs after signature: catalog access, identity-bound consumption, an agent on top. Everything runs locally and free; no cloud account.

## What it shows

| Step | File | What happens |
|---|---|---|
| Land | `ingest.py` | 8 U.S. Census retail-sales series by kind of business (via FRED, no key) to `data/raw/retail_sales.parquet`: 3,336 monthly rows, 1992 to Sep 2026 |
| Register | `ingest.py` | Iceberg table `retail.monthly_sales` in a local SQLite catalog, written as **two snapshots**: history, then the latest two months as a separate append, the way a monthly release lands |
| Query | `lake.py`, `queries.sql` | DuckDB over the Iceberg scan (Arrow); **time travel** by `snapshot_id`; a guard allows one read-only statement (SELECT / WITH / DESCRIBE / SUMMARIZE) and strips comments first |
| Serve | `server.py` | MCP server with `list_tables` and `query`. Each caller has its own token, stored only as a SHA-256 hash; a bad or revoked token fails closed; every call, including rejected writes, is appended to `audit.jsonl` with the caller's name |
| Agent | Claude Code / Desktop | Claude answers business questions through the two tools and cites the rows |

## Results from the hand-written queries (`queries.sql`, data through Sep 2026)

- Fastest year-over-year growth: gasoline stations, +28.1% ($68.0B vs $53.1B); health and personal care is the only decline, -1.2%.
- Trailing-12-month share: nonstore (e-commerce) retailers 22.3%, the largest category.
- Nonstore sales first exceeded department stores in **April 2020**.
- Time travel: snapshot 1 holds 3,320 rows through Jul 2026; the current snapshot holds 3,336 through Sep 2026.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install "pyiceberg[sql-sqlite,pyarrow]" duckdb "mcp[cli]" pytest
.venv/bin/python ingest.py
PYTHONPATH=. .venv/bin/python -m pytest -q          # 10 tests
.venv/bin/python server.py add-token acme-analyst    # prints the token once; only its hash is stored
```

Connect Claude Code (the token goes in the server's environment, never in a prompt):

```bash
claude mcp add lakehouse --env LAKE_TOKEN=<token> -- "$PWD/.venv/bin/python" "$PWD/server.py"
claude "Which U.S. store category grew fastest year over year in the latest month, and how did it differ in the earlier Iceberg snapshot? Show the SQL and cite the rows."
```

Revoke a caller without touching anyone else: `.venv/bin/python server.py revoke acme-analyst`.

## Integration runbook (how a customer would run this against their own lake)

1. **Catalog**: point `lake.catalog()` at the customer's Iceberg REST catalog (Polaris, Glue, Unity) instead of the local SQLite one; table identifiers stay the same.
2. **Identity**: issue one token per consuming user or service, never a shared one. In production, swap the static tokens for the customer's IdP (OIDC) so access follows their SSO and offboarding.
3. **Least privilege**: the server is read-only at two layers (statement guard, and an in-memory DuckDB over an Arrow scan that cannot write back to the lake).
4. **Audit**: ship `audit.jsonl` to the customer's SIEM; each line has caller, tool, SQL, snapshot and row count, and rejected statements are logged too.
5. **Reproducibility**: answers cite a `snapshot_id`, so a number in a report can be re-run against the exact data it came from.

## Limits

- Local catalog and files; not tested against a hosted Iceberg catalog, Snowflake or Databricks.
- Static tokens over stdio stand in for OAuth; a hosted deployment would put the server behind the customer's IdP.
- Public, monthly, national data; not row-level or store-level.

MIT license.
