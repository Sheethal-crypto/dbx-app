"""Deployment probe for the Vehicle Safety Signal Finder. Not the real app.

A deployed Databricks App is a plain Python process running as a service
principal. There is no Spark session and no dbutils. Everything below was
verified from a notebook, where both exist, so none of it is evidence about
the app runtime.

Check 4 is the one that matters. The agent's write tools currently build rows
with spark.createDataFrame(...).write.saveAsTable(...), and spark will not
exist in the app process, so those tools cannot work as written. The
replacement path is the SQL Statement Execution API over a SQL warehouse,
which needs no Spark session. Check 4 exercises exactly that: one insert into
workspace.vsf.app_events and a read back of the same row.

Identity is check 1 because the app runs as a service principal rather than as
the developer, and the Lakebase write failures recorded in CLAUDE.md were
identity-dependent. Every check is isolated, so one failure still leaves the
other three results on screen.

Column names for app_events are asserted in EVENT_COLUMNS below. They are not
recorded in the repo, so check 4 prints DESCRIBE TABLE first: if the insert
fails on a name, the real schema is on screen right above the traceback.
"""

import base64
import os
import traceback
import uuid

import requests
import streamlit as st
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState
from databricks.vector_search.client import VectorSearchClient

ENDPOINT_NAME = "vsf_endpoint"
INDEX_NAME = "workspace.vsf.complaint_index"
EVENTS_TABLE = "workspace.vsf.app_events"
SEARCH_COLUMNS = ["chunk_id", "text", "component_top", "model_family", "model_year"]
EVENT_COLUMNS = [
    "event_id",
    "event_ts",
    "user_id",
    "event_type",
    "tool_name",
    "payload",
    "latency_ms",
    "ok",
]
NHTSA_MODELS_URL = "https://api.nhtsa.gov/products/vehicle/models"
CHAT_MODEL = "databricks-meta-llama-3-3-70b-instruct"
SECRET_SCOPE = "vsf"
SECRET_KEY = "pat"


def render_check(label, check, heading):
    """Run one check under its own heading, catching everything. True on pass.

    The verdict placeholder is reserved before the body runs so PASS or FAIL
    sits directly under the heading rather than below the output.
    """
    heading(label)
    verdict = st.empty()
    try:
        check()
    except Exception:
        verdict.error("FAIL")
        st.code(traceback.format_exc(), language="text")
        return False
    verdict.success("PASS")
    return True


def run_sql(client, warehouse_id, statement):
    """Execute one statement and raise unless it reached SUCCEEDED."""
    response = client.statement_execution.execute_statement(
        statement=statement,
        warehouse_id=warehouse_id,
        wait_timeout="30s",
    )
    status = response.status
    state = status.state if status else None
    if state != StatementState.SUCCEEDED:
        detail = status.error.message if (status and status.error) else "no error detail"
        raise RuntimeError("statement state {}: {}".format(state, detail))
    return response


def sql_rows(response):
    """Rows from a statement response, empty list when it returned nothing."""
    result = response.result
    return list(result.data_array) if (result and result.data_array) else []


def workspace_url():
    """Workspace host with a scheme on the front.

    DATABRICKS_HOST and config.host can both come back as a bare hostname, and
    the vector search client concatenates the value straight into a URL without
    adding one, which fails as MissingSchema.
    """
    host = os.environ.get("DATABRICKS_HOST") or WorkspaceClient().config.host
    if not host.startswith("http"):
        host = "https://" + host
    return host


def check_identity():
    client = WorkspaceClient()
    me = client.current_user.me()
    st.write(
        {
            "user_name": me.user_name,
            "host": client.config.host,
            "auth_type": client.config.auth_type,
            "DATABRICKS_CLIENT_ID set": "DATABRICKS_CLIENT_ID" in os.environ,
            "DATABRICKS_WAREHOUSE_ID set": "DATABRICKS_WAREHOUSE_ID" in os.environ,
        }
    )


def check_vector_search():
    # The client cannot infer credentials inside a deployed app, so the service
    # principal is passed explicitly. These env vars exist only in the app, not
    # in a notebook, so this construction is app-only by design.
    url = workspace_url()
    st.write("workspace_url: {}".format(url))
    client = VectorSearchClient(
        workspace_url=url,
        service_principal_client_id=os.environ["DATABRICKS_CLIENT_ID"],
        service_principal_client_secret=os.environ["DATABRICKS_CLIENT_SECRET"],
        disable_notice=True,
    )
    index = client.get_index(ENDPOINT_NAME, INDEX_NAME)
    response = index.similarity_search(
        query_text="brake pedal feels soft and sinks toward the floor",
        columns=SEARCH_COLUMNS,
        filters={"model_family": "NX"},
        num_results=3,
    )

    manifest = (response or {}).get("manifest") or {}
    names = [column.get("name") for column in (manifest.get("columns") or [])]
    rows = ((response or {}).get("result") or {}).get("data_array") or []
    st.write("{} rows returned".format(len(rows)))
    if rows and names:
        st.dataframe([dict(zip(names, row)) for row in rows])
    else:
        # No rows, or no manifest to label them with. Show the raw response so
        # an empty result can be told apart from a shape change.
        st.json(response)


def check_outbound_http():
    response = requests.get(
        NHTSA_MODELS_URL,
        params={"make": "lexus", "modelYear": 2017, "issueType": "r"},
        timeout=30,
    )
    st.write("status code: {}".format(response.status_code))
    response.raise_for_status()
    # Each result is {"modelYear": ..., "make": ..., "model": ...}.
    results = response.json().get("results") or []
    st.write([item.get("model") for item in results])


def check_delta_write():
    client = WorkspaceClient()

    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    source = "DATABRICKS_WAREHOUSE_ID"
    if not warehouse_id:
        warehouses = list(client.warehouses.list())
        if not warehouses:
            raise RuntimeError(
                "DATABRICKS_WAREHOUSE_ID is unset and w.warehouses.list() is empty, "
                "so there is no warehouse to execute against."
            )
        warehouse_id = warehouses[0].id
        source = "first result of w.warehouses.list() ({})".format(warehouses[0].name)
    st.write({"warehouse_id": warehouse_id, "warehouse from": source})

    st.caption("Actual table schema, for checking the column names used below.")
    st.dataframe(sql_rows(run_sql(client, warehouse_id, "DESCRIBE TABLE " + EVENTS_TABLE)))

    event_id = str(uuid.uuid4())
    insert = (
        "INSERT INTO %s (%s) VALUES "
        "('%s', current_timestamp(), 'probe', 'probe_write', NULL, '{}', 0, true)"
        % (EVENTS_TABLE, ", ".join(EVENT_COLUMNS), event_id)
    )
    st.code(insert, language="sql")
    insert_response = run_sql(client, warehouse_id, insert)
    st.write("insert state: {}".format(insert_response.status.state))

    select_response = run_sql(
        client,
        warehouse_id,
        "SELECT count(*) FROM %s WHERE event_id = '%s'" % (EVENTS_TABLE, event_id),
    )
    rows = sql_rows(select_response)
    count = rows[0][0] if rows else None
    st.write(
        {
            "event_id": event_id,
            "select state": str(select_response.status.state),
            "rows found": count,
        }
    )
    if str(count) != "1":
        raise RuntimeError("wrote 1 row but read back {}".format(count))


def check_serving_endpoint_client():
    client = WorkspaceClient().serving_endpoints.get_open_ai_client()
    completion = client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": "Reply with the single word ready."}],
        max_tokens=10,
    )
    st.write({"model": CHAT_MODEL, "reply": completion.choices[0].message.content})


def check_secret_fallback():
    secret = WorkspaceClient().secrets.get_secret(scope=SECRET_SCOPE, key=SECRET_KEY)
    # get_secret returns base64. Only the decoded length is ever rendered.
    value = base64.b64decode(secret.value).decode("utf-8")
    st.write(
        {"scope": SECRET_SCOPE, "key": SECRET_KEY, "decoded length": len(value)}
    )


def check_chat_model():
    # Both sub-checks always run and report separately. The section as a whole
    # only fails when neither route to a chat model works.
    outcomes = [
        render_check(
            "5a. Serving endpoints OpenAI client", check_serving_endpoint_client, st.subheader
        ),
        render_check(
            "5b. Secret scope fallback ({}/{})".format(SECRET_SCOPE, SECRET_KEY),
            check_secret_fallback,
            st.subheader,
        ),
    ]
    if not any(outcomes):
        raise RuntimeError(
            "no chat model access: the serving endpoints client and the secret "
            "fallback both failed, see the two tracebacks above"
        )


CHECKS = [
    ("Identity", check_identity),
    ("Vector search", check_vector_search),
    ("Outbound HTTP", check_outbound_http),
    ("Delta write with no Spark", check_delta_write),
    ("Chat model access", check_chat_model),
]


def main():
    st.set_page_config(page_title="VSF deployment probe", layout="wide")
    st.title("Deployment probe")
    st.caption(
        "Checks what works inside a deployed Databricks App: no Spark session, "
        "no dbutils, running as a service principal."
    )

    # Each check is caught on its own so a failure never hides the rest.
    for number, (title, check) in enumerate(CHECKS, start=1):
        render_check("{}. {}".format(number, title), check, st.header)
        st.divider()


main()
