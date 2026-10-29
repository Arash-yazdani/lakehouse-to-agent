# lakehouse-to-agent

Lakehouse data served to an AI agent, end to end: public Census retail data lands as **Parquet**, is written to an **Apache Iceberg** table, is queried with **DuckDB SQL** and is served to **Claude over MCP** with per-caller tokens, read-only enforcement and an audit log.

Built in a day as the integration a data provider's solutions team runs after a contract is signed: catalog access, identity-bound consumption, an agent on top. Data, catalog and server run locally with no cloud account or API key; query results go to Claude, which runs in Anthropic's cloud.

## What it shows

| Step | File | What happens |
|---|---|---|
| Land | `ingest.py` | 8 seasonally adjusted U.S. Census retail-sales series by kind of business (via FRED, no key) to `data/raw/retail_sales.parquet`: 3,336 monthly rows, Jan 1992 to Sep 2026 |
| Register | `ingest.py` | Iceberg table `retail.monthly_sales` in a local SQLite catalog, written as **two snapshots**: history, then the latest two months as a separate append, the way a monthly release lands |
| Query | `lake.py`, `queries.sql` | DuckDB over the Iceberg scan (Arrow); **time travel** by `snapshot_id`; a guard allows one read-only statement (SELECT / WITH / DESCRIBE / SUMMARIZE) and strips comments first |
| Serve | `server.py` | MCP server with `list_tables` and `query`. Each caller has its own token, stored only as a SHA-256 hash; a bad or revoked token fails closed. Every call is appended to `audit.jsonl`: allowed, rejected and failed queries under the caller's name, denied tokens as `unknown` |
| Agent | Claude Code / Desktop | Claude answers business questions through the two tools and cites the rows |

## Results from the hand-written queries (`queries.sql`, data through Sep 2026)

- Fastest year-over-year growth in Sep 2026: gasoline stations, +28.1% ($68.0B vs $53.1B in Sep 2025); health and personal care is the only decline, -1.2%.
- Trailing-12-month share of the 7 non-overlapping categories' combined sales: nonstore retailers (mostly e-commerce) 26.0%, the largest. Grocery stores are part of food and beverage stores, so the share query leaves them out.
- Nonstore sales first exceeded food and beverage stores in **April 2020**.
- Time travel: snapshot 1 holds 3,320 rows through Jul 2026; the current snapshot holds 3,336 through Sep 2026. Asked through MCP, Claude reported gasoline stations as fastest-growing in both: +17.4% YoY as of Jul 2026 (snapshot 1), +28.1% as of Sep 2026.

## Run it

Requires Python 3.11+ (duckdb 2.0).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python ingest.py
.venv/bin/python -m pytest -q                        # 21 tests
.venv/bin/python server.py add-token acme-analyst    # prints the token once; only its hash is stored
```

Connect Claude Code (the token goes in the server's environment, never in a prompt):

```bash
claude mcp add lakehouse --env LAKE_TOKEN=<token> -- "$PWD/.venv/bin/python" "$PWD/server.py"
claude "Which U.S. store category grew fastest year over year in the latest month, and how did it differ in the earlier Iceberg snapshot? Show the SQL and cite the rows."
```

Revoke one caller; the others keep working, and the revoked token fails from its next call: `.venv/bin/python server.py revoke acme-analyst`. An unknown name exits with an error.

## Integration runbook (how a customer would run this against their own lake)

1. **Catalog**: replace the local SQLite catalog in `lake.catalog()` with the customer's Iceberg REST catalog (Polaris, Glue, Unity) and list their tables in `lake.TABLES`, or in a git-ignored `tables.local.json` for data that must stay off the repo.
2. **Identity**: issue one token per consuming user or service, never a shared one. In production, swap the static tokens for the customer's IdP (OIDC) so access follows their SSO and offboarding.
3. **Least privilege**: the server is read-only at three layers: a statement guard; DuckDB with external access disabled and its configuration locked, so SQL cannot read local files; and an in-memory DuckDB over an Arrow scan that cannot write back to the lake.
4. **Audit**: ship `audit.jsonl` to the customer's SIEM. Every line has `ts`, `caller` and `tool`. A `query` line adds the SQL and either the snapshot and row count, the guard's rejection reason, or the error. A denied token is logged as caller `unknown` with the reason and no SQL. Calls the MCP SDK rejects for malformed arguments never reach the server and are not logged.
5. **Reproducibility**: every result returns `snapshots_read`, the snapshot each table in the SQL was read at, default reads included; an unknown snapshot ID fails instead of reading current data, so a number in a report can be re-run against the exact data it came from. In this demo a reload rebuilds the table; a production feed would land corrections as new snapshots under a retention policy, so cited IDs stay valid.

## Found while testing

- Iceberg snapshot ids (e.g. `7514425504626583672`) exceed JSON's exact-integer range (2^53). Sent as a number over MCP, the id was rounded to `...583552`, and time travel missed. The server now returns and accepts snapshot ids as strings (regression test included).
- SQL that starts with a `--` comment was rejected by the read-only guard; agents write that often. Comments are stripped before the check.
- The agent flagged that the series labeled department stores ran at about $85B a month, too large for department stores. FRED confirms `RSDBS` is Food and Beverage Stores, so the label in `ingest.py` was wrong. Grocery stores (`RSGCS`) are part of it, so the share query now leaves them out instead of counting them twice.
- Found in a code review: the read-only guard allowed SELECT over DuckDB's file readers, so `read_json('audit.jsonl')`, `read_csv('tokens.json')` or `read_text` on any local file passed it, and any caller could read other callers' SQL, the token hashes or files on the host. DuckDB now runs with external access and extension autoloading disabled and its configuration locked (regression tests). The same review added audit lines for denied tokens and failed queries.
- Default reads returned and logged no snapshot ID, so their numbers could not be re-run. Every result now cites the snapshot it read (regression test).
- The share-query fix lived only in my hand-written SQL. `list_tables` now tells the agent that grocery stores are part of food and beverage stores.
- A rejected statement reached the agent as a bare `Error executing tool query`: the MCP SDK hides the message of any exception that isn't a `ToolError`. The server now raises `ToolError` with the guard's reason, so the agent can rewrite its query; the audit log already recorded the reason (regression test included).
- Found in a second review: a result of exactly 500 rows was flagged `truncated`, and `revoke` with a misspelled caller name exited 0 and left the real token active (regression tests for both).

## Limits

- Local catalog and files; not tested against a hosted Iceberg catalog, Snowflake or Databricks.
- Static tokens over stdio stand in for OAuth; a hosted deployment would put the server behind the customer's IdP. Tokens carry no data scope: any valid token can query every table, so entitlements per contract are not modeled.
- Every query loads the full table into memory through an Arrow scan; fine at 3,336 rows, production sizes need filter pushdown. There is no query timeout.
- Both snapshots come from one FRED pull: snapshot 1 holds the current, revised values through Jul 2026 and does not reproduce what Census published in August. A later pull adds months and revisions, so the numbers above change. `ingest.py` rebuilds the warehouse, so earlier snapshot IDs do not survive a reload.
- Public monthly national aggregates; no row-level or store-level data.

MIT license.
