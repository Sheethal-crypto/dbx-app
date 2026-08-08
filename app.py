"""Vehicle Safety Signal Finder. Streamlit frontend for the Databricks App.

All data access goes through vsf_tools, which is shared with the notebook so
there is one implementation rather than two that drift. Nothing here talks to
Delta, vector search or NHTSA directly.

Two things about that module shape the code below:

- The tools wrapped in timed_tool take keyword arguments only, because the
  wrapper is defined as wrapper(**kwargs). Positional calls raise TypeError.
- list_vehicles, tool_usage and recent_events come back as lists of lists from
  the SQL statement API, with every value a string. Rows are zipped into dicts
  and numbers are parsed defensively at render time.

The deployment probe that established what works inside an app lives in
probe.py.
"""

import html
import traceback

import streamlit as st

st.set_page_config(
    page_title="Vehicle Safety Signal Finder",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

try:
    import vsf_tools as vsf

    IMPORT_ERROR = None
except Exception:  # noqa: BLE001 - surfaced in the UI rather than crashing the page
    vsf = None
    IMPORT_ERROR = traceback.format_exc()


SCREENS = ["Garage", "Ask", "Insights"]
VEHICLE_FIELDS = ("vehicle_id", "vin", "make", "model_raw", "model_family", "model_year")
DISCLAIMER = (
    "This reports what NHTSA records contain. It is not a diagnosis and it is "
    "not repair advice."
)

# System font stacks only. Outbound access is restricted to a trusted domain
# list, so a webfont request would hang rather than fall back cleanly.
CSS = """
<style>
:root {
  --ink: #16181A;
  --ink-soft: #4A4F55;
  --ink-faint: #7A8088;
  --paper: #FAF9F7;
  --card: #FFFFFF;
  --line: #E4DFD8;
  --line-strong: #CFC8BE;
  --accent: #1F3A5F;
  --accent-dark: #172C48;
  --amber: #8A6116;
  --amber-bg: #FBF4E6;
  --amber-line: #E6D3AC;
  --red: #8C2F2A;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}

[data-testid="stDecoration"] { display: none; }
[data-testid="stHeader"] { background: transparent; }

html, body, [class*="css"] { font-family: var(--sans); }

.block-container { max-width: 1080px; padding-top: 2.2rem; padding-bottom: 3rem; }

h1 { font-size: 1.55rem; font-weight: 600; letter-spacing: -0.015em; color: var(--ink); }
h2 { font-size: 1.1rem;  font-weight: 600; letter-spacing: -0.005em; color: var(--ink); }
h3 { font-size: 0.95rem; font-weight: 600; color: var(--ink); }

.vsf-eyebrow {
  font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.1em;
  color: var(--ink-faint); font-weight: 600; margin-bottom: 0.35rem;
}
.vsf-lede { font-size: 0.86rem; line-height: 1.6; color: var(--ink-soft); margin-bottom: 1.2rem; }
.vsf-rule { border-top: 1px solid var(--line); margin: 1.6rem 0 1.2rem; }

/* sidebar */
[data-testid="stSidebar"] { border-right: 1px solid var(--line); }
[data-testid="stSidebar"] .block-container { padding-top: 2rem; }
.vsf-wordmark {
  font-size: 0.74rem; font-weight: 700; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--accent); line-height: 1.5;
}
.vsf-wordmark span { display: block; color: var(--ink-faint); font-weight: 600; }
.vsf-sidenote {
  font-size: 0.72rem; line-height: 1.55; color: var(--ink-soft);
  border-top: 1px solid var(--line); padding-top: 0.7rem; margin-top: 0.9rem;
}
.vsf-sidenote b { color: var(--ink); font-weight: 600; }
.vsf-sidenote .mono { font-family: var(--mono); font-size: 0.68rem; color: var(--ink-faint); }

/* controls */
.stButton > button {
  border-radius: 2px; border: 1px solid var(--line-strong); background: var(--card);
  color: var(--ink); font-size: 0.8rem; font-weight: 500; padding: 0.35rem 0.95rem;
}
.stButton > button:hover { border-color: var(--accent); color: var(--accent); }
.stButton > button[kind="primary"] {
  background: var(--accent); border-color: var(--accent); color: #FFFFFF;
}
.stButton > button[kind="primary"]:hover { background: var(--accent-dark); border-color: var(--accent-dark); }
.stButton > button:disabled { opacity: 0.4; }
.stTextInput input, .stTextArea textarea {
  border-radius: 2px; border: 1px solid var(--line-strong); background: var(--card);
  font-size: 0.88rem; color: var(--ink);
}
.stTextArea textarea { line-height: 1.6; }
.stTextInput input { font-family: var(--mono); letter-spacing: 0.04em; }

/* cards */
.vsf-card {
  background: var(--card); border: 1px solid var(--line); border-radius: 3px;
  padding: 0.85rem 1rem 0.7rem; margin-bottom: 0.6rem;
}
.vsf-card.is-selected { border-color: var(--accent); box-shadow: inset 3px 0 0 0 var(--accent); }
.vsf-card.is-other { border-left: 3px solid var(--amber); }
.vsf-vehicle-title { font-size: 0.95rem; font-weight: 600; color: var(--ink); letter-spacing: -0.005em; }
.vsf-vehicle-meta {
  font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--ink-faint); margin-top: 0.15rem;
}
.vsf-vin {
  font-family: var(--mono); font-size: 0.72rem; letter-spacing: 0.05em; color: var(--ink-soft);
  border-top: 1px solid var(--line); margin-top: 0.6rem; padding-top: 0.5rem;
}

/* complaint cards */
.vsf-head { display: flex; justify-content: space-between; align-items: baseline; gap: 0.8rem; }
.vsf-component {
  font-size: 0.78rem; font-weight: 700; letter-spacing: 0.05em;
  text-transform: uppercase; color: var(--ink);
}
.vsf-year { font-family: var(--mono); font-size: 0.74rem; color: var(--ink-soft); white-space: nowrap; }
.vsf-narrative { font-size: 0.8rem; line-height: 1.85; color: var(--ink-soft); margin: 0.7rem 0 0.6rem; }
.vsf-complaint-id {
  font-family: var(--mono); font-size: 0.68rem; letter-spacing: 0.05em; color: var(--ink-faint);
  border-top: 1px solid var(--line); padding-top: 0.5rem;
}
.vsf-badge {
  display: inline-block; font-size: 0.62rem; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--amber); background: var(--amber-bg);
  border: 1px solid var(--amber-line); border-radius: 2px; padding: 0.1rem 0.4rem;
  margin-bottom: 0.5rem;
}

/* tables */
.vsf-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
.vsf-table th {
  text-align: left; font-size: 0.64rem; text-transform: uppercase; letter-spacing: 0.09em;
  font-weight: 600; color: var(--ink-faint); border-bottom: 1px solid var(--line-strong);
  padding: 0.45rem 0.6rem;
}
.vsf-table td { padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--line); color: var(--ink); }
.vsf-table td.num, .vsf-table th.num { font-family: var(--mono); text-align: right; }
.vsf-table td.mono { font-family: var(--mono); font-size: 0.74rem; color: var(--ink-soft); }
.vsf-table .fail { color: var(--red); font-weight: 600; }
.vsf-table .zero { color: var(--ink-faint); }

.vsf-empty {
  border: 1px dashed var(--line-strong); border-radius: 3px; padding: 1.2rem;
  font-size: 0.84rem; color: var(--ink-soft); background: var(--card);
}
.vsf-footer {
  margin-top: 2.5rem; padding-top: 0.9rem; border-top: 1px solid var(--line);
  font-size: 0.72rem; line-height: 1.6; color: var(--ink-faint);
}
</style>
"""


# ------------------------------------------------------------------- helpers

def esc(value):
    """Escape for HTML. Narratives are raw NHTSA text and contain anything."""
    return html.escape("" if value is None else str(value))


def attempt(label, fn, **kwargs):
    """Call a tool, rendering failures inline. Returns (ok, value)."""
    try:
        return True, fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 - the page must survive any tool failure
        st.error("{} failed: {}".format(label, exc))
        with st.expander("Error detail"):
            st.code(traceback.format_exc(), language="text")
        return False, None


def fmt_num(value):
    """SQL returns everything as a string. Show an integer when it is one."""
    try:
        return "{:,}".format(int(float(value)))
    except (TypeError, ValueError):
        return esc(value)


def fmt_ts(value):
    """Trim a timestamp to seconds without pulling in a date parser."""
    text = "" if value is None else str(value)
    return esc(text.replace("T", " ")[:19])


def as_vehicle(row):
    return dict(zip(VEHICLE_FIELDS, row))


def vehicle_title(vehicle):
    parts = [
        vehicle.get("model_year"),
        vehicle.get("make"),
        vehicle.get("model_raw") or vehicle.get("model_family"),
    ]
    title = " ".join(str(part) for part in parts if part).strip()
    return title or "Unknown vehicle"


def selected_vehicle():
    vehicle_id = st.session_state.selected_vehicle_id
    for row in st.session_state.vehicles or []:
        vehicle = as_vehicle(row)
        if vehicle["vehicle_id"] == vehicle_id:
            return vehicle
    return None


def load_vehicles(force=False):
    if force or st.session_state.vehicles is None:
        with st.spinner("Loading garage"):
            ok, rows = attempt("Loading vehicles", vsf.list_vehicles)
        st.session_state.vehicles = rows if ok else []


def render_table(headers, rows):
    """Rows are lists of (html, css_class) pairs, already escaped."""
    head = "".join(
        "<th class='{}'>{}</th>".format(cls, esc(text)) for text, cls in headers
    )
    body = "".join(
        "<tr>{}</tr>".format(
            "".join("<td class='{}'>{}</td>".format(cls, cell) for cell, cls in row)
        )
        for row in rows
    )
    st.markdown(
        "<table class='vsf-table'><thead><tr>{}</tr></thead><tbody>{}</tbody></table>".format(
            head, body
        ),
        unsafe_allow_html=True,
    )


def eyebrow(text):
    st.markdown("<div class='vsf-eyebrow'>{}</div>".format(esc(text)), unsafe_allow_html=True)


def lede(text):
    st.markdown("<div class='vsf-lede'>{}</div>".format(esc(text)), unsafe_allow_html=True)


def rule():
    st.markdown("<div class='vsf-rule'></div>", unsafe_allow_html=True)


# ------------------------------------------------------------------- garage

def render_garage():
    st.markdown("# Garage")
    lede(
        "Add a vehicle by VIN. The VIN is decoded through NHTSA vPIC and the model "
        "is folded to the family used across the complaint corpus, which is what "
        "lets a decoded VIN match indexed complaints at all."
    )

    with st.form("add_vehicle", clear_on_submit=False):
        left, right = st.columns([4, 1])
        with left:
            vin = st.text_input(
                "VIN",
                placeholder="17 character VIN",
                label_visibility="collapsed",
            )
        with right:
            submitted = st.form_submit_button("Add vehicle", type="primary", use_container_width=True)

    if submitted:
        vin = (vin or "").strip().upper()
        if not vin:
            st.warning("Enter a VIN first.")
        else:
            # Two NHTSA calls plus a warehouse round trip. p95 is about 9.5 seconds.
            with st.spinner("Decoding VIN and saving to your garage"):
                ok, added = attempt("Add vehicle", vsf.add_vehicle, vin=vin)
            if ok:
                load_vehicles(force=True)
                st.session_state.selected_vehicle_id = added.get("vehicle_id")
                st.success(
                    "Added {}. Selected for search.".format(vehicle_title(added))
                )

    rule()

    load_vehicles()
    rows = st.session_state.vehicles or []
    eyebrow("Saved vehicles")

    if not rows:
        st.markdown(
            "<div class='vsf-empty'>No vehicles saved yet. Add a VIN above to begin.</div>",
            unsafe_allow_html=True,
        )
        return

    columns = st.columns(3)
    for position, row in enumerate(rows):
        vehicle = as_vehicle(row)
        vehicle_id = vehicle["vehicle_id"]
        is_selected = vehicle_id == st.session_state.selected_vehicle_id
        with columns[position % 3]:
            st.markdown(
                "<div class='vsf-card {selected}'>"
                "<div class='vsf-vehicle-title'>{title}</div>"
                "<div class='vsf-vehicle-meta'>Family {family}{marker}</div>"
                "<div class='vsf-vin'>{vin}</div>"
                "</div>".format(
                    selected="is-selected" if is_selected else "",
                    title=esc(vehicle_title(vehicle)),
                    family=esc(vehicle.get("model_family") or "unknown"),
                    marker=" &middot; Selected" if is_selected else "",
                    vin=esc(vehicle.get("vin")),
                ),
                unsafe_allow_html=True,
            )
            if is_selected:
                st.button("Selected", key="sel_{}".format(vehicle_id), disabled=True,
                          use_container_width=True)
            elif st.button("Select", key="sel_{}".format(vehicle_id), use_container_width=True):
                st.session_state.selected_vehicle_id = vehicle_id
                vsf.log_event(
                    "vehicle_selected",
                    payload={"vehicle_id": vehicle_id, "model_family": vehicle.get("model_family")},
                )
                st.rerun()


# ---------------------------------------------------------------------- ask

def render_complaint_card(match, vehicle, scoped):
    """One complaint. Marked as a different vehicle only when the search was
    unscoped, since a scoped search cannot return another family."""
    family = (match.get("model_family") or "").upper()
    wanted = ((vehicle or {}).get("model_family") or "").upper()
    is_other = bool(not scoped and wanted and family and family != wanted)

    badge = ""
    if is_other:
        badge = "<div class='vsf-badge'>Different vehicle &middot; {}</div>".format(esc(family))

    st.markdown(
        "<div class='vsf-card {other}'>"
        "{badge}"
        "<div class='vsf-head'>"
        "<div class='vsf-component'>{component}</div>"
        "<div class='vsf-year'>{family} {year}</div>"
        "</div>"
        "<div class='vsf-narrative'>{narrative}</div>"
        "<div class='vsf-complaint-id'>Complaint {complaint_id}</div>"
        "</div>".format(
            other="is-other" if is_other else "",
            badge=badge,
            component=esc(match.get("component") or "Component not recorded"),
            family=esc(family),
            year=esc(match.get("model_year") or ""),
            narrative=esc(match.get("narrative")),
            complaint_id=esc(match.get("complaint_id")),
        ),
        unsafe_allow_html=True,
    )


def render_ask():
    load_vehicles()
    vehicle = selected_vehicle()

    st.markdown("# Ask")
    lede(
        "Describe the problem the way you would to a mechanic. Owner complaints are "
        "written in the same plain language, while the component taxonomy is not."
    )

    if vehicle is None:
        st.warning(
            "No vehicle selected. Choose one in Garage to scope results to your car. "
            "Searching without one returns complaints from every vehicle in the corpus."
        )

    symptom = st.text_area(
        "Symptom",
        value=st.session_state.symptom,
        height=110,
        placeholder="For example: shudders around 40 mph when the transmission shifts",
        label_visibility="collapsed",
    )
    st.session_state.symptom = symptom

    controls, action = st.columns([3, 1])
    with controls:
        scoped = st.toggle(
            "Scope to my vehicle",
            value=st.session_state.scoped,
            disabled=vehicle is None,
            help="Filters the search to your vehicle's model family. Unfiltered "
                 "search returns the wrong vehicle 97 percent of the time.",
        )
        st.session_state.scoped = scoped
    with action:
        searching = st.button("Search complaints", type="primary", use_container_width=True)

    scope_family = vehicle.get("model_family") if (scoped and vehicle) else None

    if searching:
        if not symptom.strip():
            st.warning("Describe the symptom first.")
        else:
            with st.spinner("Searching complaints"):
                ok, matches = attempt(
                    "Search",
                    vsf.search_complaints,
                    symptom=symptom.strip(),
                    model_family=scope_family,
                    num_results=5,
                )
            if ok:
                st.session_state.results = matches or []
                st.session_state.results_symptom = symptom.strip()
                st.session_state.results_scoped = bool(scope_family)
                st.session_state.agent_answer = None

    results = st.session_state.results
    if not results:
        return

    rule()
    eyebrow(
        "Matching complaints, scoped to {}".format(
            (vehicle or {}).get("model_family", "")
        )
        if st.session_state.results_scoped
        else "Matching complaints, whole corpus"
    )
    for match in results:
        render_complaint_card(match, vehicle, st.session_state.results_scoped)

    save_column, note_column = st.columns([1, 3])
    with save_column:
        save = st.button(
            "Save to vehicle",
            disabled=vehicle is None,
            use_container_width=True,
        )
    with note_column:
        if vehicle is None:
            st.caption("Select a vehicle in Garage to save this report.")
        else:
            st.caption(
                "Saves the symptom and these {} complaints against {}.".format(
                    len(results), vehicle_title(vehicle)
                )
            )

    if save and vehicle is not None:
        with st.spinner("Saving report"):
            ok, saved = attempt(
                "Save report",
                vsf.save_symptom_report,
                vehicle_id=vehicle["vehicle_id"],
                symptom=st.session_state.results_symptom,
                matches=results,
            )
        if ok:
            st.success(
                "Saved report {} with {} matches.".format(
                    saved.get("report_id", ""), saved.get("saved_matches", 0)
                )
            )

    rule()
    eyebrow("Agent")
    st.markdown(
        "<div class='vsf-lede'>The agent answers the same question with tools of its "
        "own. It searches complaints, and checks whether NHTSA has an open recall "
        "campaign covering the component.</div>",
        unsafe_allow_html=True,
    )

    if st.button("Ask the agent about this symptom"):
        if vehicle is not None:
            question = "My {} (model family {}). {}".format(
                vehicle_title(vehicle),
                vehicle.get("model_family"),
                st.session_state.results_symptom,
            )
        else:
            question = st.session_state.results_symptom
        with st.spinner("The agent is working. It may call several tools."):
            ok, answer = attempt("Agent", vsf.run_agent, user_message=question)
        st.session_state.agent_answer = answer if ok else None

    if st.session_state.agent_answer:
        with st.container(border=True):
            st.markdown(st.session_state.agent_answer)


# ----------------------------------------------------------------- insights

def render_insights():
    st.markdown("# Insights")
    lede(
        "Every tool call and user action appends a row to workspace.vsf.app_events, "
        "a Delta table with change data feed enabled. These numbers are read back "
        "from that log, so the app reports on itself."
    )

    eyebrow("Tool usage")
    with st.spinner("Reading the event log"):
        ok, usage = attempt("Tool usage", vsf.tool_usage)

    if ok and usage:
        rows = []
        for tool_name, calls, p50, p95, failures in usage:
            failed = fmt_num(failures)
            rows.append(
                [
                    (esc(tool_name), "mono"),
                    (fmt_num(calls), "num"),
                    (fmt_num(p50), "num"),
                    (fmt_num(p95), "num"),
                    (
                        "<span class='{}'>{}</span>".format(
                            "zero" if failed == "0" else "fail", failed
                        ),
                        "num",
                    ),
                ]
            )
        render_table(
            [("Tool", ""), ("Calls", "num"), ("p50 ms", "num"), ("p95 ms", "num"),
             ("Failures", "num")],
            rows,
        )
    elif ok:
        st.markdown(
            "<div class='vsf-empty'>No tool calls logged yet.</div>",
            unsafe_allow_html=True,
        )

    rule()
    eyebrow("Recent events")
    with st.spinner("Reading recent events"):
        ok, events = attempt("Recent events", vsf.recent_events, limit=25)

    if ok and events:
        rows = []
        for event_ts, event_type, tool_name, latency_ms, is_ok in events:
            healthy = str(is_ok).strip().lower() in ("true", "1")
            rows.append(
                [
                    (fmt_ts(event_ts), "mono"),
                    (esc(event_type), ""),
                    (esc(tool_name or ""), "mono"),
                    (fmt_num(latency_ms), "num"),
                    (
                        "ok" if healthy else "<span class='fail'>failed</span>",
                        "",
                    ),
                ]
            )
        render_table(
            [("Time, UTC", ""), ("Event", ""), ("Tool", ""), ("Latency ms", "num"),
             ("Result", "")],
            rows,
        )
    elif ok:
        st.markdown(
            "<div class='vsf-empty'>No events logged yet.</div>",
            unsafe_allow_html=True,
        )


# --------------------------------------------------------------------- shell

def init_state():
    defaults = {
        "screen": SCREENS[0],
        "vehicles": None,
        "selected_vehicle_id": None,
        "symptom": "",
        "scoped": True,
        "results": [],
        "results_symptom": "",
        "results_scoped": True,
        "agent_answer": None,
        "logged_screen": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def render_sidebar():
    with st.sidebar:
        st.markdown(
            "<div class='vsf-wordmark'>Vehicle Safety<span>Signal Finder</span></div>",
            unsafe_allow_html=True,
        )
        st.markdown("<div class='vsf-rule'></div>", unsafe_allow_html=True)
        st.radio("Screen", SCREENS, key="screen", label_visibility="collapsed")

        vehicle = selected_vehicle()
        if vehicle:
            note = (
                "<div class='vsf-sidenote'>Selected vehicle<br><b>{}</b><br>"
                "<span class='mono'>{}</span></div>"
            ).format(esc(vehicle_title(vehicle)), esc(vehicle.get("vin")))
        else:
            note = (
                "<div class='vsf-sidenote'>No vehicle selected. Results will not be "
                "scoped to your car.</div>"
            )
        st.markdown(note, unsafe_allow_html=True)


def main():
    st.markdown(CSS, unsafe_allow_html=True)

    if IMPORT_ERROR:
        st.error("The shared tool module failed to load, so no data is available.")
        st.code(IMPORT_ERROR, language="text")
        return

    init_state()
    render_sidebar()

    screen = st.session_state.screen
    # One row per navigation, not per rerun, so the log stays readable.
    if screen != st.session_state.logged_screen:
        st.session_state.logged_screen = screen
        vsf.log_event("page_view", payload={"screen": screen})

    if screen == "Garage":
        render_garage()
    elif screen == "Ask":
        render_ask()
    else:
        render_insights()

    st.markdown(
        "<div class='vsf-footer'>{}</div>".format(esc(DISCLAIMER)),
        unsafe_allow_html=True,
    )


main()
