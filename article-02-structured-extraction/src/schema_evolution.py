"""Schema evolution: graceful field addition.

Columns are registered with the extraction_version that introduced them.
Adding a clause (e.g. governing_law in 2.0) means one registry entry:
the Pydantic model gets the field, the table gets an ALTER TABLE ADD COLUMN,
old rows keep NULL, and dbt models COALESCE. No backfill, no rewrite.
"""

from typing import Dict, List, Tuple

# name -> (snowflake type, introduced_in_version)
CORE_COLUMNS: Dict[str, Tuple[str, float]] = {
    "contract_date": ("DATE", 1.0),
    "payment_terms": ("VARCHAR(2000)", 1.0),
    "termination_conditions": ("VARCHAR(4000)", 1.0),
    "liability_cap": ("VARCHAR(2000)", 1.0),
    "confidence_scores": ("VARIANT", 1.0),
    "page_references": ("VARIANT", 1.0),
    "source_spans": ("VARIANT", 1.0),
    "extraction_method": ("VARCHAR(50)", 1.0),
    "ocr_confidence": ("FLOAT", 1.0),
    "prompt_version": ("VARCHAR(100)", 1.0),
    "model_name": ("VARCHAR(100)", 1.0),
    "processing_timestamp": ("TIMESTAMP_NTZ", 1.0),
    "routing": ("VARCHAR(30)", 1.0),
    "review_required": ("BOOLEAN", 1.0),
    "validation_flags": ("VARIANT", 1.0),
    # Evolved columns
    "governing_law": ("VARCHAR(500)", 2.0),
}

VARIANT_COLUMNS = {n for n, (t, _) in CORE_COLUMNS.items() if t == "VARIANT"}
KEY_COLUMNS = ["contract_id", "extraction_version"]


def columns_for_version(version: float) -> List[str]:
    """Data columns valid for a given extraction_version."""
    return [n for n, (_, since) in CORE_COLUMNS.items() if since <= version]


def all_data_columns() -> List[str]:
    return list(CORE_COLUMNS)


def create_table_sql(schema: str, table: str = "extracted_contracts", version: float = 1.0) -> str:
    cols = ",\n    ".join(f"{n} {CORE_COLUMNS[n][0]}" for n in columns_for_version(version))
    return (
        f"CREATE TABLE IF NOT EXISTS {schema}.{table} (\n"
        f"    contract_id VARCHAR(100),\n"
        f"    extraction_version FLOAT,\n"
        f"    {cols},\n"
        f"    created_timestamp TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),\n"
        f"    last_updated TIMESTAMP_NTZ,\n"
        f"    PRIMARY KEY (contract_id, extraction_version)\n);"
    )


def evolve_table(cursor, schema: str, table: str = "extracted_contracts") -> List[str]:
    """Add any registered column missing from the live table. Returns added names."""
    cursor.execute(
        "SELECT LOWER(column_name) FROM information_schema.columns "
        "WHERE table_schema = %(schema)s AND table_name = %(table)s",
        {"schema": schema.upper(), "table": table.upper()},
    )
    existing = {r[0] for r in cursor.fetchall()}
    added = []
    for name, (sql_type, _) in CORE_COLUMNS.items():
        if name not in existing:
            cursor.execute(f"ALTER TABLE {schema}.{table} ADD COLUMN IF NOT EXISTS {name} {sql_type}")
            added.append(name)
    return added
