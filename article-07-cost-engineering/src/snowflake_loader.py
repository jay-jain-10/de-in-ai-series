"""Creates the cost_events table in Snowflake (connection injected)."""

from pathlib import Path

DDL_PATH = Path(__file__).resolve().parent.parent / "sql" / "cost_events_ddl.sql"


def load_ddl(schema: str = "RAW") -> str:
    return DDL_PATH.read_text().replace("{schema}", schema)


def create_cost_events_table(connection, schema: str = "RAW") -> None:
    cur = connection.cursor()
    try:
        sql = "\n".join(l for l in load_ddl(schema).splitlines() if not l.strip().startswith("--"))
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
    finally:
        cur.close()
