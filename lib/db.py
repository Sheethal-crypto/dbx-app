import os
import psycopg
import streamlit as st
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from databricks.sdk import WorkspaceClient

SCHEMA = os.environ.get("APP_SCHEMA", "support")


@st.cache_resource
def get_pool() -> ConnectionPool:
    workspace = WorkspaceClient()
    endpoint = os.environ["ENDPOINT_NAME"]

    class OAuthConnection(psycopg.Connection):
        @classmethod
        def connect(cls, conninfo="", **kwargs):
            cred = workspace.postgres.generate_database_credential(endpoint=endpoint)
            kwargs["password"] = cred.token
            return super().connect(conninfo, **kwargs)

    host = os.environ.get("LAKEBASE_HOST") or os.environ["PGHOST"]
    conninfo = (
        f"host={host} "
        f"port={os.environ.get('PGPORT', '5432')} "
        f"dbname={os.environ['PGDATABASE']} "
        f"user={os.environ['PGUSER']} "
        f"sslmode={os.environ.get('PGSSLMODE', 'require')}"
    )

    return ConnectionPool(
        conninfo=conninfo,
        connection_class=OAuthConnection,
        min_size=1,
        max_size=2,
        timeout=15,
        open=True,
    )


def query(sql: str, params: tuple = ()) -> list[dict]:
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def execute(sql: str, params: tuple = ()) -> None:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
