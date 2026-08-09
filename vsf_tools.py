"""Shared tool layer for the Vehicle Safety Signal Finder.

Imported by both the Databricks App and the development notebook, so there is one
implementation rather than two that drift.

Two constraints shape everything here:

1. **No Spark session.** Databricks Apps run a plain Python process. Writes go
   through the SQL statement execution API against a warehouse.
2. **Identity varies.** In a notebook this runs as the user; in the app it runs as a
   service principal. `WorkspaceClient()` resolves either, and vector search needs
   service principal credentials passed explicitly when they are present.

All writes use bound parameters rather than interpolated SQL. Complaint narratives and
user-typed symptoms are full of apostrophes, which would break interpolated inserts and
would be an injection hole besides.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import requests
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem, StatementState

CATALOG = "workspace"
SCHEMA = "vsf"
FQ = f"{CATALOG}.{SCHEMA}"

ENDPOINT_NAME = "vsf_endpoint"
INDEX_NAME = f"{FQ}.complaint_index"
CHAT_MODEL = "databricks-meta-llama-3-3-70b-instruct"

SEARCH_COLUMNS = ["chunk_id", "text", "component_top", "model_family", "model_year"]

NHTSA_RECALLS = "https://api.nhtsa.gov/recalls/recallsByVehicle"
NHTSA_MODELS = "https://api.nhtsa.gov/products/vehicle/models"
VPIC_DECODE = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"

_w = WorkspaceClient()


# --------------------------------------------------------------------- plumbing

def workspace_url() -> str:
    """Host with a scheme.

    `WorkspaceClient().config.host` returns a bare hostname in some contexts, and the
    vector search client concatenates it into a URL without adding one, producing
    `MissingSchema`.
    """
    raw = os.environ.get("DATABRICKS_HOST") or _w.config.host or ""
    return raw if raw.startswith("http") else f"https://{raw}"


_warehouse_id: str | None = None


def warehouse_id() -> str:
    """Resolve a SQL warehouse once and cache it.

    Prefers `DATABRICKS_WAREHOUSE_ID` so the app can pin one explicitly rather than
    depending on list ordering.
    """
    global _warehouse_id
    if _warehouse_id:
        return _warehouse_id

    env = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if env:
        _warehouse_id = env
        return _warehouse_id

    warehouses = list(_w.warehouses.list())
    if not warehouses:
        raise RuntimeError("No SQL warehouse is available to this identity")
    _warehouse_id = warehouses[0].id
    return _warehouse_id


def run_sql(statement: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
    """Execute SQL with bound parameters and return rows as a list of lists.

    The statement API reports SQL failures in the response status rather than raising,
    so anything other than SUCCEEDED is turned into an exception here. Without that, a
    failed insert looks like a successful one.
    """
    items = None
    if params:
        items = [StatementParameterListItem(name=k, value=None if v is None else str(v))
                 for k, v in params.items()]

    resp = _w.statement_execution.execute_statement(
        warehouse_id=warehouse_id(),
        statement=statement,
        parameters=items,
        wait_timeout="30s",
    )

    state = resp.status.state if resp.status else None
    if state != StatementState.SUCCEEDED:
        detail = resp.status.error.message if resp.status and resp.status.error else ""
        raise RuntimeError(f"SQL {state}: {detail}")

    if resp.result and resp.result.data_array:
        return [list(r) for r in resp.result.data_array]
    return []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(sep=" ", timespec="seconds")


# ------------------------------------------------------------------ http retry

RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 0.5
RETRY_STATUS = {429, 500, 502, 503, 504}


def _get_with_retry(url, params=None, timeout=20):
    """GET with exponential backoff on transient failures.

    Retries connection errors, timeouts, 429 and 5xx, then raises. Every other
    status is handed back to the caller untouched, which is what keeps the recalls
    loop fast: a 400 there means NHTSA does not recognize that model name, which is
    an answer rather than a failure, and retrying it would turn one correct fast
    skip into three slow ones.

    Raising on exhaustion rather than returning a sentinel keeps the failure on the
    path timed_tool already logs to app_events.
    """
    delay = RETRY_BACKOFF
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code not in RETRY_STATUS:
                return r
            reason = "HTTP {}".format(r.status_code)
        except (requests.ConnectionError, requests.Timeout) as exc:
            reason = type(exc).__name__
        if attempt == RETRY_ATTEMPTS:
            raise requests.RequestException(
                "{} failed after {} attempts: {}".format(url, RETRY_ATTEMPTS, reason))
        time.sleep(delay)
        delay *= 2


# ------------------------------------------------------------------- event log

USER_ID = "demo"


def log_event(event_type, tool_name=None, payload=None, latency_ms=None, ok=True):
    """One row per user or agent action. Failures here are swallowed: losing an
    analytics row must never break the action it was recording."""
    try:
        run_sql(
            f"""INSERT INTO {FQ}.app_events
                (event_id, event_ts, user_id, event_type, tool_name, payload, latency_ms, ok)
                VALUES (:event_id, current_timestamp(), :user_id, :event_type,
                        :tool_name, :payload, CAST(:latency_ms AS BIGINT), CAST(:ok AS BOOLEAN))""",
            {
                "event_id": str(uuid.uuid4()),
                "user_id": USER_ID,
                "event_type": event_type,
                "tool_name": tool_name,
                "payload": json.dumps(payload or {}, default=str)[:4000],
                "latency_ms": int(latency_ms) if latency_ms is not None else 0,
                "ok": "true" if ok else "false",
            },
        )
    except Exception:
        pass


def timed_tool(fn: Callable) -> Callable:
    """Log every tool call with its latency and outcome."""
    def wrapper(**kwargs):
        t0 = time.time()
        try:
            out = fn(**kwargs)
            log_event("tool_call", fn.__name__, {"args": kwargs},
                      int((time.time() - t0) * 1000), True)
            return out
        except Exception as e:
            log_event("tool_call", fn.__name__, {"args": kwargs, "error": str(e)[:500]},
                      int((time.time() - t0) * 1000), False)
            raise
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# --------------------------------------------------------------- vector search

_index = None


def index():
    """Vector search index handle.

    The client cannot infer credentials inside a deployed app, so the service
    principal is passed explicitly when its environment variables are present. In a
    notebook they are absent and the default resolution works.
    """
    global _index
    if _index is not None:
        return _index

    from databricks.vector_search.client import VectorSearchClient

    cid = os.environ.get("DATABRICKS_CLIENT_ID")
    secret = os.environ.get("DATABRICKS_CLIENT_SECRET")

    if cid and secret:
        client = VectorSearchClient(
            workspace_url=workspace_url(),
            service_principal_client_id=cid,
            service_principal_client_secret=secret,
            disable_notice=True,
        )
    else:
        client = VectorSearchClient(disable_notice=True)

    _index = client.get_index(ENDPOINT_NAME, INDEX_NAME)
    return _index


def to_model_family(model_txt: str | None) -> str:
    """Same normalization the Silver layer applies.

    A vPIC decode returns the base model, and the complaints file records the same
    vehicle under many strings. Folding both to one family is what makes a decoded VIN
    match indexed complaints at all.
    """
    m = (model_txt or "").upper().strip()
    m = re.sub(r"^REDUNDANT\s+", "", m)
    m = re.sub(r"\s+(HYBRID|PRIME|PLUG-IN)$", "", m)
    if re.match(r"^[A-Z]{2}[0-9]", m):
        m = m[:2]
    return m.strip()


# ----------------------------------------------------------------- read tools

@timed_tool
def search_complaints(symptom, model_family=None, num_results=5):
    """Find NHTSA owner complaints matching a described symptom, optionally scoped to
    one vehicle line."""
    # Over-fetch a small margin to absorb near-duplicate narratives. Doubling was
    # wasteful: deduplication rarely removes more than one or two.
    kwargs = dict(query_text=symptom, columns=SEARCH_COLUMNS, num_results=num_results + 3)
    if model_family:
        kwargs["filters"] = {"model_family": model_family}

    res = index().similarity_search(**kwargs)
    rows = res.get("result", {}).get("data_array", []) or []

    seen, out = set(), []
    for r in rows:
        text = r[1] or ""
        key = text[:200]
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "complaint_id": r[0],
            "narrative": text[:800],
            "component": r[2],
            "model_family": r[3],
            "model_year": r[4],
        })
        if len(out) >= num_results:
            break
    return out


def _resolve_recall_models(make, model_family, model_year):
    """NHTSA's recall database uses different model strings than the complaints file.
    A 2017 Lexus NX is NX200T, NX300H or NX200 there, never NX, and querying NX returns
    a 400. Ask the API what it knows, then match the family against it."""
    r = _get_with_retry(NHTSA_MODELS,
                        params={"make": make, "modelYear": model_year, "issueType": "r"})
    r.raise_for_status()
    known = {x["model"].upper() for x in r.json().get("results", []) if x.get("model")}
    fam = (model_family or "").upper()
    exact = [m for m in known if m == fam]
    prefixed = sorted(m for m in known if m.startswith(fam) and m != fam)
    return exact + prefixed


@timed_tool
def lookup_recalls(make, model_family, model_year):
    """Open recall campaigns for a vehicle, resolving the model family to whichever
    strings NHTSA's recall database actually uses."""
    candidates = _resolve_recall_models(make, model_family, model_year)
    if not candidates:
        return {"models_tried": [], "recalls": []}

    seen, out = set(), []
    for m in candidates[:4]:
        r = _get_with_retry(NHTSA_RECALLS,
                            params={"make": make, "model": m, "modelYear": model_year})
        if r.status_code != 200:
            continue
        for x in r.json().get("results", []) or []:
            cid = x.get("NHTSACampaignNumber")
            if cid in seen:
                continue
            seen.add(cid)
            out.append({
                "campaign": cid,
                "model": m,
                "component": x.get("Component"),
                "summary": (x.get("Summary") or "")[:600],
                "remedy": (x.get("Remedy") or "")[:400],
            })
    return {"models_tried": candidates[:4], "recalls": out[:6]}


# ---------------------------------------------------------------- write tools

# I, O and Q are never used in a VIN, so they are excluded rather than being
# treated as typos for 1 and 0.
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def _normalize_vin(vin: str | None) -> str:
    """Uppercase, with surrounding whitespace, inner spaces and hyphens removed.

    Owners read a VIN off a registration document or a windscreen photo, so it
    arrives lowercase and grouped. Normalizing before the existence check is what
    makes the same car typed two different ways resolve to one row.
    """
    return re.sub(r"[\s\-]", "", (vin or "").strip().upper())


def _find_vehicle(vin: str) -> dict | None:
    """This user's earliest saved row for a VIN, or None.

    Earliest rather than latest so a garage that already carries duplicates keeps
    returning the same vehicle_id the cleanup statement would have kept.
    """
    rows = run_sql(
        f"""SELECT vehicle_id, vin, make, model_raw, model_family, model_year
            FROM {FQ}.app_vehicles
            WHERE user_id = :user_id AND vin = :vin
            ORDER BY created_at LIMIT 1""",
        {"user_id": USER_ID, "vin": vin},
    )
    if not rows:
        return None

    row = dict(zip(
        ("vehicle_id", "vin", "make", "model_raw", "model_family", "model_year"),
        rows[0],
    ))
    # The statement API returns every column as a string.
    year = row.get("model_year")
    row["model_year"] = int(float(year)) if year not in (None, "") else None
    return row


@timed_tool
def decode_vin(vin):
    """Decode a VIN through vPIC. No write.

    Split from the save so a caller can show the owner their car as soon as vPIC
    answers, rather than holding the screen for the warehouse round trip too.

    Returns a status of `ok`, `invalid` or `not_found`. Validating here rather than
    at the save covers both callers: the app decodes before it saves, so a bad VIN
    is refused without spending a vPIC call or a warehouse round trip on it.
    """
    vin = _normalize_vin(vin)
    if not VIN_RE.match(vin):
        return {
            "status": "invalid",
            "message": ("A VIN is exactly 17 characters, letters and digits only, and "
                        "never contains I, O or Q. Check the one you entered."),
            "vin": vin,
        }

    r = _get_with_retry(VPIC_DECODE.format(vin=vin))
    r.raise_for_status()
    d = r.json()["Results"][0]

    model_raw = d.get("Model") or ""
    year_raw = d.get("ModelYear") or ""
    decoded = {
        "status": "ok",
        "vin": vin,
        "make": (d.get("Make") or "").upper(),
        "model_raw": model_raw,
        "model_family": to_model_family(model_raw),
        "model_year": int(year_raw) if str(year_raw).isdigit() else None,
    }

    # vPIC answers 200 with empty fields for a VIN it cannot place, so an empty
    # make or model is the only signal that the decode found nothing.
    if not decoded["make"] or not decoded["model_family"]:
        decoded["status"] = "not_found"
        decoded["message"] = "NHTSA vPIC could not identify a vehicle for that VIN."
    return decoded


@timed_tool
def save_vehicle(decoded):
    """Write an already decoded vehicle to the garage, unless the user already has it.

    The existence check sits here rather than in `add_vehicle` because this is the
    function that inserts, and the app reaches it directly. Returns a status of
    `added` or `exists`; `exists` is a success and carries the existing vehicle_id.
    """
    vin = _normalize_vin(decoded.get("vin"))

    existing = _find_vehicle(vin)
    if existing:
        return dict(existing, status="exists",
                    message="That VIN is already in your garage.")

    vehicle_id = str(uuid.uuid4())

    run_sql(
        f"""INSERT INTO {FQ}.app_vehicles
            (vehicle_id, user_id, vin, make, model_raw, model_family, model_year, created_at)
            VALUES (:vehicle_id, :user_id, :vin, :make, :model_raw, :model_family,
                    CAST(:model_year AS INT), current_timestamp())""",
        {
            "vehicle_id": vehicle_id, "user_id": USER_ID,
            "vin": vin, "make": decoded["make"],
            "model_raw": decoded["model_raw"], "model_family": decoded["model_family"],
            "model_year": decoded["model_year"],
        },
    )

    return dict(decoded, vin=vin, vehicle_id=vehicle_id, status="added",
                message="Vehicle added to your garage.")


@timed_tool
def add_vehicle(vin):
    """Decode a VIN through vPIC and save the vehicle to the user's garage.

    Kept as one call for the agent, which has no use for the intermediate state.

    Returns a status of `added`, `exists`, `invalid` or `not_found`. Calling it twice
    with the same VIN is safe: the second call returns the first call's vehicle_id.
    """
    decoded = decode_vin(vin=vin)
    if decoded.get("status") != "ok":
        return decoded
    return save_vehicle(decoded=decoded)


@timed_tool
def save_symptom_report(vehicle_id, symptom, matches=None):
    """Save a described symptom and the complaints matched to it.

    Written together because a report without its matches is not worth keeping.
    """
    report_id = str(uuid.uuid4())

    run_sql(
        f"""INSERT INTO {FQ}.app_symptom_reports
            (report_id, vehicle_id, symptom, created_at)
            VALUES (:report_id, :vehicle_id, :symptom, current_timestamp())""",
        {"report_id": report_id, "vehicle_id": vehicle_id, "symptom": symptom},
    )

    matches = matches or []
    for m in matches:
        run_sql(
            f"""INSERT INTO {FQ}.app_matches
                (match_id, report_id, complaint_id, component_top, score, accepted, created_at)
                VALUES (:match_id, :report_id, :complaint_id, :component_top,
                        CAST(:score AS DOUBLE), NULL, current_timestamp())""",
            {
                "match_id": str(uuid.uuid4()),
                "report_id": report_id,
                "complaint_id": m.get("complaint_id"),
                "component_top": m.get("component"),
                "score": float(m.get("score") or 0.0),
            },
        )

    return {"report_id": report_id, "saved_matches": len(matches)}


# -------------------------------------------------------------------- queries

def list_vehicles():
    return run_sql(
        f"""SELECT vehicle_id, vin, make, model_raw, model_family, model_year
            FROM {FQ}.app_vehicles WHERE user_id = :user_id
            ORDER BY created_at DESC""",
        {"user_id": USER_ID},
    )


def tool_usage():
    return run_sql(
        f"""SELECT tool_name,
                   count(*) AS calls,
                   round(percentile_approx(latency_ms, 0.5)) AS p50_ms,
                   round(percentile_approx(latency_ms, 0.95)) AS p95_ms,
                   sum(CASE WHEN ok THEN 0 ELSE 1 END) AS failures
            FROM {FQ}.app_events
            WHERE tool_name IS NOT NULL
            GROUP BY tool_name ORDER BY calls DESC"""
    )


def recent_events(limit=25):
    return run_sql(
        f"""SELECT event_ts, event_type, tool_name, latency_ms, ok
            FROM {FQ}.app_events ORDER BY event_ts DESC LIMIT {int(limit)}"""
    )


# ---------------------------------------------------------------------- agent

TOOLS = {
    "search_complaints": search_complaints,
    "lookup_recalls": lookup_recalls,
    "add_vehicle": add_vehicle,
    "save_symptom_report": save_symptom_report,
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "search_complaints",
        "description": ("Find NHTSA owner complaints matching a described symptom. "
                        "Always pass model_family when the vehicle is known, otherwise "
                        "results come back from a different vehicle."),
        "parameters": {"type": "object", "properties": {
            "symptom": {"type": "string"},
            "model_family": {"type": "string"},
        }, "required": ["symptom"]}}},
    {"type": "function", "function": {
        "name": "lookup_recalls",
        "description": ("Check NHTSA for open recall campaigns. Pass the model family "
                        "such as NX or RX; the tool resolves it to the model strings "
                        "the recall database uses."),
        "parameters": {"type": "object", "properties": {
            "make": {"type": "string"},
            "model_family": {"type": "string"},
            "model_year": {"type": "integer"},
        }, "required": ["make", "model_family", "model_year"]}}},
    {"type": "function", "function": {
        "name": "add_vehicle",
        "description": "Decode a VIN and save the vehicle to the user's garage.",
        "parameters": {"type": "object", "properties": {
            "vin": {"type": "string"},
        }, "required": ["vin"]}}},
    {"type": "function", "function": {
        "name": "save_symptom_report",
        "description": "Save a described symptom and the complaints matched to it.",
        "parameters": {"type": "object", "properties": {
            "vehicle_id": {"type": "string"},
            "symptom": {"type": "string"},
        }, "required": ["vehicle_id", "symptom"]}}},
]

SYSTEM = (
    "You help vehicle owners find out whether a problem they are experiencing has been "
    "reported by other owners of the same vehicle, and whether NHTSA has an open recall "
    "for it. Always scope complaint searches to the user's vehicle. Cite complaint ids. "
    "Say plainly that this reports what NHTSA records contain and is not a diagnosis or "
    "repair advice. "
    "Report only what the records contain. Never suggest a cause, a repair, a part to "
    "replace, or how urgent something is, even if asked. If the records do not answer "
    "something, say so rather than filling the gap."
)

_chat = None


def _serving_token() -> str:
    """A token for the serving endpoints REST API.

    DATABRICKS_TOKEN when the environment sets one, otherwise the vsf/pat secret,
    which the app service principal has READ on. get_secret returns base64.
    """
    token = os.environ.get("DATABRICKS_TOKEN")
    if token:
        return token
    secret = _w.secrets.get_secret(scope="vsf", key="pat")
    return base64.b64decode(secret.value).decode("utf-8")


def chat_client():
    """OpenAI compatible client authenticated as whatever identity is running: the
    user in a notebook, the service principal in the app.

    get_open_ai_client is preferred because it needs no token, but the SDK pinned
    in the deployed container is older than the notebook runtime's and does not
    have it: 'ServingEndpointsAPI' object has no attribute 'get_open_ai_client'.
    Where it is missing, point the OpenAI client at the serving endpoints base
    URL and authenticate explicitly.
    """
    global _chat
    if _chat is not None:
        return _chat

    factory = getattr(_w.serving_endpoints, "get_open_ai_client", None)
    if callable(factory):
        _chat = factory()
        return _chat

    from openai import OpenAI

    _chat = OpenAI(
        api_key=_serving_token(),
        base_url=f"{workspace_url()}/serving-endpoints",
    )
    return _chat


def run_agent(user_message, max_steps=5):
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_message}]
    t0 = time.time()
    client = chat_client()

    for _ in range(max_steps):
        resp = client.chat.completions.create(
            model=CHAT_MODEL, messages=messages,
            tools=TOOL_SCHEMAS, max_tokens=900,
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            log_event("agent_answer", None, {"question": user_message[:500]},
                      int((time.time() - t0) * 1000), True)
            return msg.content

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            try:
                result = TOOLS[name](**args)
            except Exception as e:
                # Named so the model reports an unavailable lookup rather than
                # reading a failed call as an absence of records.
                result = {"status": "unavailable", "tool": name,
                          "error": str(e)[:300],
                          "note": "This lookup did not complete. Do not report it as "
                                  "an absence of records."}
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, default=str)[:6000]})

    return "Stopped after the maximum number of tool calls."
