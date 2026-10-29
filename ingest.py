"""FRED retail sales by store type -> Parquet -> Apache Iceberg table (local SQLite catalog).

Writes two snapshots: history, then the latest two months as a separate append, the way a
monthly Census release lands, so time travel has an earlier snapshot to read.

    python ingest.py            # fetch from FRED (no key needed), write data/ and warehouse/
"""
from __future__ import annotations

import csv
import io
import urllib.request
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pyiceberg.catalog.sql import SqlCatalog

ROOT = Path(__file__).parent
WAREHOUSE = ROOT / "warehouse"
RAW = ROOT / "data" / "raw" / "retail_sales.parquet"
TABLE = "retail.monthly_sales"

# U.S. Census Monthly Retail Trade, sales by kind of business, millions of dollars, seasonally adjusted, via FRED.
SERIES = {
    "RSFSDP": "Food services and drinking places",
    "RSGCS": "Grocery stores",
    "RSNSR": "Nonstore retailers",
    "RSGASS": "Gasoline stations",
    "RSDBS": "Food and beverage stores",   # FRED RSDBS is Food and Beverage Stores; RSGCS is part of it
    "RSHPCS": "Health and personal care stores",
    "RSCCAS": "Clothing and accessory stores",
    "RSBMGESD": "Building materials and garden",
}

SCHEMA = pa.schema([
    pa.field("month", pa.date32(), nullable=False),
    pa.field("series_id", pa.string(), nullable=False),
    pa.field("category", pa.string(), nullable=False),
    pa.field("sales_musd", pa.int64(), nullable=False),
])


def fetch(series_id: str) -> list[tuple[date, int]]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    with urllib.request.urlopen(url, timeout=30) as r:
        rows = csv.DictReader(io.StringIO(r.read().decode()))
        return [(date.fromisoformat(x["observation_date"]), int(float(x[series_id])))
                for x in rows if x[series_id] not in ("", ".")]


def to_table(series: dict[str, list[tuple[date, int]]]) -> pa.Table:
    rows = [(m, sid, SERIES[sid], v) for sid, obs in series.items() for m, v in obs]
    rows.sort()
    return pa.Table.from_arrays([pa.array(c) for c in zip(*rows)], schema=SCHEMA)


def split_latest(table: pa.Table, months: int = 2) -> tuple[pa.Table, pa.Table]:
    """(history, latest `months` months) so the latest release lands as its own snapshot."""
    cutoffs = sorted(set(table.column("month").to_pylist()))[-months:]
    mask = pc.is_in(table.column("month"), pa.array(cutoffs, pa.date32()))
    return table.filter(pc.invert(mask)), table.filter(mask)


def load(table: pa.Table, warehouse: Path = WAREHOUSE) -> SqlCatalog:
    # Replace only this table: other tables in the warehouse (for example local licensed data) survive a re-ingest.
    warehouse.mkdir(parents=True, exist_ok=True)
    catalog = SqlCatalog("local", uri=f"sqlite:///{warehouse}/catalog.db",
                         warehouse=f"file://{warehouse}")
    if catalog.table_exists(TABLE):
        catalog.purge_table(TABLE)
    if ("retail",) not in catalog.list_namespaces():
        catalog.create_namespace("retail")
    ice = catalog.create_table(TABLE, schema=SCHEMA)
    history, latest = split_latest(table)
    ice.append(history)   # snapshot 1
    ice.append(latest)    # snapshot 2: the "new release"
    return catalog


def main() -> None:
    table = to_table({sid: fetch(sid) for sid in SERIES})
    RAW.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, RAW)
    ice = load(table).load_table(TABLE)
    snaps = [(s.snapshot_id, s.summary.additional_properties.get("added-records")) for s in ice.snapshots()]
    print(f"{table.num_rows} rows, {len(SERIES)} categories, "
          f"{table.column('month').to_pylist()[0]} to {max(table.column('month').to_pylist())}")
    print(f"parquet: {RAW}\niceberg: {TABLE} snapshots {snaps}")


if __name__ == "__main__":
    main()
