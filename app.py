import os
import traceback
import streamlit as st

st.title("Connection diagnostic")

st.write({
    "ENDPOINT_NAME": os.environ.get("ENDPOINT_NAME"),
    "LAKEBASE_HOST": os.environ.get("LAKEBASE_HOST"),
    "PGHOST": os.environ.get("PGHOST"),
    "PGUSER": os.environ.get("PGUSER"),
    "PGDATABASE": os.environ.get("PGDATABASE"),
})

st.subheader("1. SDK client")
try:
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    st.success("WorkspaceClient created")
    st.write("has .postgres:", hasattr(w, "postgres"))
    st.write("has .database:", hasattr(w, "database"))
    import databricks.sdk
    st.write("sdk version:", databricks.sdk.version.__version__)
except Exception:
    st.error("WorkspaceClient failed")
    st.code(traceback.format_exc())
    st.stop()

st.subheader("2. Generate credential")
try:
    cred = w.postgres.generate_database_credential(
        endpoint=os.environ["ENDPOINT_NAME"]
    )
    token = cred.token
    st.success(f"Token received, length {len(token)}")
except Exception:
    st.error("Credential generation failed")
    st.code(traceback.format_exc())
    st.stop()

st.subheader("3. Direct connect")
host = os.environ.get("LAKEBASE_HOST") or os.environ["PGHOST"]
try:
    import psycopg
    conn = psycopg.connect(
        host=host,
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=token,
        sslmode="require",
        connect_timeout=15,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT current_user, current_database()")
        st.success(cur.fetchone())
    conn.close()
except Exception:
    st.error(f"Connect to {host} failed")
    st.code(traceback.format_exc())
