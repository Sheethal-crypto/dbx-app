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

Latency, in the order the user feels it:

- The serverless warehouse auto-stops, and the first statement pays the start
  cost. A background SELECT 1 fires once per session so that cost lands before
  the user asks for anything.
- Adding a vehicle is two steps, decode then write. The decoded car is drawn as
  soon as vPIC answers, and the warehouse write follows underneath it.
- Searches are cached on (symptom, model_family, scoped), so flipping the scope
  toggle back is instant rather than another round trip.

All artwork is inline SVG and all motion is CSS. Outbound access is restricted
to a trusted domain list, so a webfont or remote image would hang rather than
fail gracefully.

The deployment probe that established what works inside an app lives in
probe.py.
"""

import html
import threading
import traceback
from collections import Counter

import streamlit as st

st.set_page_config(
    page_title="Vehicle Safety Signal Finder",
    page_icon=None,
    layout="centered",
    initial_sidebar_state="collapsed",
)

try:
    import vsf_tools as vsf

    IMPORT_ERROR = None
except Exception:  # noqa: BLE001 - surfaced in the UI rather than crashing the page
    vsf = None
    IMPORT_ERROR = traceback.format_exc()


SCREENS = ["Garage", "Ask", "Insights"]
VEHICLE_FIELDS = ("vehicle_id", "vin", "make", "model_raw", "model_family", "model_year")
# vsf_tools caps narratives with text[:800], which lands mid-word. Matching the
# number here is what lets the cut be moved back to a word boundary.
NARRATIVE_LIMIT = 800
NUM_RESULTS = 5
ACCENT = "#0F766E"
DISCLAIMER = (
    "This reports what NHTSA records contain. It is not a diagnosis and it is "
    "not repair advice."
)

# System font stacks only. Outbound access is restricted to a trusted domain
# list, so a webfont request would hang rather than fall back cleanly.
CSS = """
<style>
:root {
  --ink: #1A1A1A;
  --ink-soft: #6B6B6B;
  --page: #F6F4F1;
  --card: #FFFFFF;
  --hairline: #EEEBE7;
  --field: #E3DFDA;
  --accent: #0F766E;
  --accent-dark: #0B5C55;
  --accent-wash: rgba(15, 118, 110, 0.14);
  --amber: #B45309;
  --amber-wash: #FDF6EC;
  --amber-ring: rgba(180, 83, 9, 0.35);
  --red: #B91C1C;
  --skeleton: #EDEAE5;
  --skeleton-sheen: #F5F2EE;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  --lift: 0 1px 2px rgba(0, 0, 0, 0.05), 0 8px 20px rgba(0, 0, 0, 0.04);
  --lift-hover: 0 2px 4px rgba(0, 0, 0, 0.06), 0 14px 30px rgba(0, 0, 0, 0.07);
}

[data-testid="stDecoration"], [data-testid="stSidebar"],
[data-testid="collapsedControl"] { display: none; }
[data-testid="stHeader"] { background: transparent; height: 0; }
.stApp { background: var(--page); }

html, body, [class*="css"] { font-family: var(--sans); font-size: 17px; color: var(--ink); }

.block-container {
  max-width: 1040px; margin: 0 auto;
  padding: 1.1rem 1.25rem 4rem;
}

h1 { font-size: 34px; font-weight: 700; letter-spacing: -0.022em; color: var(--ink);
     margin: 0 0 8px; line-height: 1.2; }
h2 { font-size: 21px; font-weight: 650; color: var(--ink); }
h3 { font-size: 18px; font-weight: 650; color: var(--ink); }
p, li, label, .stMarkdown { font-size: 17px; }

/* header, with a low opacity teal wash bleeding out to the page edges */
.vsf-header {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; flex-wrap: wrap;
  margin: 0 -1.25rem; padding: 14px 1.25rem 16px;
  background: linear-gradient(180deg, rgba(15, 118, 110, 0.07) 0%,
                                      rgba(15, 118, 110, 0.02) 55%,
                                      rgba(15, 118, 110, 0) 100%);
}
.vsf-brand { font-size: 22px; font-weight: 700; letter-spacing: -0.018em; color: var(--ink); }
.vsf-chip {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--accent); color: #FFFFFF; border-radius: 999px;
  padding: 9px 18px; font-size: 15px; font-weight: 600; letter-spacing: 0.01em;
  box-shadow: var(--lift);
}
.vsf-chip.is-empty { background: var(--card); color: var(--ink-soft); font-weight: 500; }

/* tabs, tightened against the header above and the content below */
.stTabs [data-baseweb="tab-list"] {
  gap: 30px; background: transparent; border-bottom: 1px solid var(--hairline);
  margin-bottom: 14px;
}
.stTabs [data-baseweb="tab"] {
  height: 42px; padding: 0; background: transparent;
  font-size: 17px; font-weight: 550; color: var(--ink-soft);
}
.stTabs [data-baseweb="tab"][aria-selected="true"] { color: var(--ink); font-weight: 650; }
.stTabs [data-baseweb="tab-highlight"] { background-color: var(--accent); height: 2px; }
.stTabs [data-baseweb="tab-border"] { display: none; }
.stTabs [data-baseweb="tab-panel"] { padding-top: 4px; }

/* text blocks */
.vsf-eyebrow {
  font-size: 14px; font-weight: 650; letter-spacing: 0.02em;
  color: var(--ink-soft); margin: 0 0 10px;
}
.vsf-lede { font-size: 17px; line-height: 1.6; color: var(--ink-soft); margin: 0 0 20px; }
.vsf-rule { height: 24px; }

/* cards */
.vsf-card {
  background: var(--card); border: none; border-radius: 14px;
  padding: 24px; margin-bottom: 16px; box-shadow: var(--lift);
  transition: transform 150ms ease, box-shadow 150ms ease;
  animation: vsf-rise 220ms ease both;
}
.vsf-card:hover { transform: translateY(-2px); box-shadow: var(--lift-hover); }
.vsf-card.is-selected { box-shadow: 0 0 0 2px var(--accent), var(--lift); }
.vsf-card.is-selected:hover { box-shadow: 0 0 0 2px var(--accent), var(--lift-hover); }
.vsf-card.is-other { box-shadow: 0 0 0 1px var(--amber-ring), var(--lift); }
.vsf-card.is-other:hover { box-shadow: 0 0 0 1px var(--amber-ring), var(--lift-hover); }
@keyframes vsf-rise { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; } }

.vsf-vehicle-title { font-size: 18px; font-weight: 650; color: var(--ink); letter-spacing: -0.01em; }
.vsf-vehicle-meta { font-size: 15px; color: var(--ink-soft); margin-top: 4px; }
.vsf-vin {
  font-family: var(--mono); font-size: 14px; letter-spacing: 0.02em;
  color: var(--ink-soft); margin-top: 14px;
}

/* complaint cards */
.vsf-head { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; }
.vsf-component { font-size: 16px; font-weight: 650; color: var(--ink); }
.vsf-year { font-size: 15px; color: var(--ink-soft); white-space: nowrap; }
/* The narrative is the content of the card, so it takes the primary colour.
   All caps at low contrast is harder to read than the size alone suggests. */
.vsf-narrative {
  font-size: 16px; line-height: 1.7; letter-spacing: 0.015em;
  color: var(--ink); margin: 14px 0 16px;
}
.vsf-complaint-id { font-family: var(--mono); font-size: 14px; color: var(--ink-soft); }
.vsf-badge {
  display: inline-block; font-size: 14px; font-weight: 600; color: var(--amber);
  background: var(--amber-wash); border-radius: 999px; padding: 4px 12px; margin-bottom: 12px;
}

/* skeletons, shown while a search is in flight */
.vsf-skeleton {
  background: var(--card); border-radius: 14px; padding: 24px; margin-bottom: 16px;
  box-shadow: var(--lift);
}
.vsf-sk-line {
  height: 12px; border-radius: 6px; margin-bottom: 12px;
  background: linear-gradient(90deg, var(--skeleton) 0%, var(--skeleton-sheen) 50%,
                                     var(--skeleton) 100%);
  background-size: 200% 100%;
  animation: vsf-shimmer 1.4s ease-in-out infinite;
}
.vsf-sk-line.w40 { width: 40%; height: 15px; }
.vsf-sk-line.w95 { width: 95%; }
.vsf-sk-line.w88 { width: 88%; }
.vsf-sk-line.w60 { width: 60%; }
.vsf-sk-line.w22 { width: 22%; margin-bottom: 0; margin-top: 18px; height: 10px; }
@keyframes vsf-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* controls */
.stButton > button, .stFormSubmitButton > button {
  min-height: 44px; border-radius: 10px; padding: 0 22px;
  font-size: 16px; font-weight: 600; letter-spacing: 0.005em;
  background: var(--card); color: var(--ink); border: 1px solid var(--field);
  box-shadow: none; transition: none;
}
.stButton > button:hover, .stFormSubmitButton > button:hover {
  border-color: var(--ink-soft); color: var(--ink);
}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
  background: var(--accent); border-color: var(--accent); color: #FFFFFF;
}
.stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover {
  background: var(--accent-dark); border-color: var(--accent-dark); color: #FFFFFF;
}
.stButton > button:disabled, .stButton > button:disabled:hover { opacity: 0.45; }

[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="base-input"] {
  background: var(--card); border-radius: 12px; border: 1px solid var(--field);
}
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within {
  border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-wash);
}
.stTextInput input, .stTextArea textarea {
  background: var(--card); border: none; font-size: 17px; color: var(--ink);
}
.stTextArea textarea { padding: 14px; line-height: 1.6; }
.stTextInput input { padding: 12px 14px; font-family: var(--mono); letter-spacing: 0.04em; }
.stTextInput input::placeholder, .stTextArea textarea::placeholder { color: #A3A3A3; }
[data-testid="stWidgetLabel"] p, [data-testid="stCaptionContainer"] p { font-size: 15px; }

/* tables */
.vsf-table { width: 100%; border-collapse: collapse; font-size: 16px; }
.vsf-table th {
  text-align: left; font-size: 14px; font-weight: 600; color: var(--ink-soft);
  padding: 8px 12px 14px; border-bottom: 1px solid var(--hairline);
}
.vsf-table td {
  padding: 16px 12px; border-bottom: 1px solid var(--hairline); color: var(--ink);
}
.vsf-table tr:last-child td { border-bottom: none; }
.vsf-table td.num, .vsf-table th.num { font-variant-numeric: tabular-nums; text-align: right; }
.vsf-table td.mono { font-family: var(--mono); font-size: 15px; color: var(--ink-soft); }
.vsf-table .fail { color: var(--red); font-weight: 600; }
.vsf-table .zero { color: var(--ink-soft); }

/* Streamlit wraps every vertical block, including each tab body and each
   column, in stVerticalBlockBorderWrapper. Styling it turns all of them into
   surfaces, which nests cards inside cards. Only the classes below get a
   surface: the page background carries everything else. */

.vsf-empty {
  display: flex; align-items: center; gap: 22px;
  background: var(--card); border-radius: 14px; box-shadow: var(--lift);
  padding: 28px; font-size: 17px; color: var(--ink-soft);
  animation: vsf-rise 220ms ease both;
}
.vsf-empty svg { flex: none; }
.vsf-empty-title { font-size: 18px; font-weight: 650; color: var(--ink); margin-bottom: 4px; }
.vsf-empty-body { line-height: 1.6; }

.vsf-footer {
  margin-top: 44px; padding-top: 20px; border-top: 1px solid var(--hairline);
  font-size: 15px; line-height: 1.6; color: var(--ink-soft);
}
</style>
"""

# Inline SVG. No external requests, and the strokes inherit the palette above.
GARAGE_EMPTY_SVG = """
<svg width="76" height="76" viewBox="0 0 76 76" fill="none" aria-hidden="true">
  <rect x="6" y="34" width="64" height="24" rx="6" stroke="#CFC8BE" stroke-width="2"/>
  <path d="M14 34l7-13a6 6 0 015.3-3.2h23.4A6 6 0 0155 21l7 13" stroke="#CFC8BE" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="21" cy="58" r="5" stroke="#0F766E" stroke-width="2"/>
  <circle cx="55" cy="58" r="5" stroke="#0F766E" stroke-width="2"/>
  <path d="M16 44h8M52 44h8" stroke="#CFC8BE" stroke-width="2" stroke-linecap="round"/>
</svg>
"""

NO_RESULTS_SVG = """
<svg width="76" height="76" viewBox="0 0 76 76" fill="none" aria-hidden="true">
  <rect x="14" y="10" width="38" height="50" rx="5" stroke="#CFC8BE" stroke-width="2"/>
  <path d="M22 24h22M22 33h22M22 42h13" stroke="#CFC8BE" stroke-width="2" stroke-linecap="round"/>
  <circle cx="50" cy="48" r="13" fill="#F6F4F1" stroke="#0F766E" stroke-width="2"/>
  <path d="M59 57l7 7" stroke="#0F766E" stroke-width="2" stroke-linecap="round"/>
</svg>
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


def fmt_year(value):
    """The vector index returns model_year as a float, so 2014 arrives as 2014.0."""
    if value is None or value == "":
        return ""
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value)


def clip_words(text):
    """Move a mid-word truncation back to the last complete word."""
    text = (text or "").strip()
    if len(text) < NARRATIVE_LIMIT:
        return text
    head = text[:NARRATIVE_LIMIT].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return head + "..."


def sentence_case(text):
    """Component names arrive shouting, as SERVICE BRAKES. Parenthesised
    acronyms keep their capitals, so (ESC) does not become (esc)."""
    words = [
        word if word.startswith("(") else word.lower()
        for word in (text or "").split()
    ]
    joined = " ".join(words)
    return joined[:1].upper() + joined[1:]


def fmt_ts(value):
    """Trim a timestamp to seconds without pulling in a date parser."""
    text = "" if value is None else str(value)
    return esc(text.replace("T", " ")[:19])


def as_vehicle(row):
    return dict(zip(VEHICLE_FIELDS, row))


def vehicle_title(vehicle):
    """Year, make and model. The make is shouted in the source data; model codes
    such as NX and RAV4 are genuinely uppercase and stay that way."""
    parts = [
        fmt_year(vehicle.get("model_year")),
        (vehicle.get("make") or "").title(),
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


def warm_warehouse():
    """Fire one trivial statement per session, off the main thread.

    The serverless warehouse auto-stops, and whichever statement runs first pays
    the start cost. That is most of the 9.5 second add_vehicle p95, so it is
    better spent while the user is still reading the page. Failures are ignored:
    this is an optimisation, and the real call will report its own errors.
    """
    if st.session_state.get("warmed"):
        return
    st.session_state.warmed = True

    def _warm():
        try:
            vsf.run_sql("SELECT 1")
        except Exception:  # noqa: BLE001 - a failed warm-up must stay invisible
            pass

    # Daemon so a slow warehouse start cannot hold the process open. The thread
    # touches no Streamlit API, only vsf_tools, so it needs no script context.
    threading.Thread(target=_warm, daemon=True).start()


@st.cache_data(ttl=300, show_spinner=False)
def cached_search(symptom, model_family, scoped):
    """Search results keyed on the exact scope the user asked for.

    scoped is redundant with model_family being None, but it is part of the key
    by design: toggling scope off and back on returns the first result set from
    cache rather than paying for the round trip again.
    """
    del scoped  # key only
    return vsf.search_complaints(
        symptom=symptom, model_family=model_family, num_results=NUM_RESULTS
    )


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
        "<div class='vsf-card'><table class='vsf-table'>"
        "<thead><tr>{}</tr></thead><tbody>{}</tbody></table></div>".format(head, body),
        unsafe_allow_html=True,
    )


def eyebrow(text):
    st.markdown("<div class='vsf-eyebrow'>{}</div>".format(esc(text)), unsafe_allow_html=True)


def lede(text):
    st.markdown("<div class='vsf-lede'>{}</div>".format(esc(text)), unsafe_allow_html=True)


def rule():
    st.markdown("<div class='vsf-rule'></div>", unsafe_allow_html=True)


def empty_state(svg, title, body):
    st.markdown(
        "<div class='vsf-empty'>{svg}<div><div class='vsf-empty-title'>{title}</div>"
        "<div class='vsf-empty-body'>{body}</div></div></div>".format(
            svg=svg, title=esc(title), body=esc(body)
        ),
        unsafe_allow_html=True,
    )


def render_skeletons(count):
    """Placeholder cards with the shape of the real ones, shown while a search
    is in flight. The real cards fade in over the top when they replace these."""
    card = (
        "<div class='vsf-skeleton'>"
        "<div class='vsf-sk-line w40'></div>"
        "<div class='vsf-sk-line w95'></div>"
        "<div class='vsf-sk-line w88'></div>"
        "<div class='vsf-sk-line w60'></div>"
        "<div class='vsf-sk-line w22'></div>"
        "</div>"
    )
    st.markdown(card * count, unsafe_allow_html=True)


# ------------------------------------------------------------------- garage

def render_vehicle_card(vehicle, is_selected, pending=False):
    st.markdown(
        "<div class='vsf-card {selected}'>"
        "<div class='vsf-vehicle-title'>{title}</div>"
        "<div class='vsf-vehicle-meta'>Model family {family}{marker}</div>"
        "<div class='vsf-vin'>{vin}</div>"
        "</div>".format(
            selected="is-selected" if is_selected else "",
            title=esc(vehicle_title(vehicle)),
            family=esc(vehicle.get("model_family") or "unknown"),
            marker=" &middot; Saving" if pending else (" &middot; Selected" if is_selected else ""),
            vin=esc(vehicle.get("vin")),
        ),
        unsafe_allow_html=True,
    )


def render_garage():
    st.markdown("# Garage")
    lede(
        "Add a vehicle by VIN. The VIN is decoded through NHTSA vPIC and the model "
        "is folded to the family used across the complaint records, which is what "
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
            # Two steps so the car appears at vPIC speed, roughly 2 seconds,
            # rather than after the warehouse write as well.
            preview = st.empty()
            with st.spinner("Decoding VIN"):
                ok, decoded = attempt("Decode VIN", vsf.decode_vin, vin=vin)
            if ok:
                with preview.container():
                    render_vehicle_card(decoded, is_selected=False, pending=True)
                with st.spinner("Saving to your garage"):
                    saved_ok, added = attempt("Save vehicle", vsf.save_vehicle, decoded=decoded)
                preview.empty()
                if saved_ok:
                    load_vehicles(force=True)
                    st.session_state.selected_vehicle_id = added.get("vehicle_id")
                    st.success("Added {}. Selected for search.".format(vehicle_title(added)))

    rule()

    load_vehicles()
    rows = st.session_state.vehicles or []
    eyebrow("Saved vehicles")

    if not rows:
        empty_state(
            GARAGE_EMPTY_SVG,
            "No vehicles yet",
            "Add a VIN above and it will appear here, ready to scope your searches.",
        )
        return

    columns = st.columns(2)
    for position, row in enumerate(rows):
        vehicle = as_vehicle(row)
        vehicle_id = vehicle["vehicle_id"]
        is_selected = vehicle_id == st.session_state.selected_vehicle_id
        with columns[position % 2]:
            render_vehicle_card(vehicle, is_selected)
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
            component=esc(sentence_case(match.get("component")) or "Component not recorded"),
            family=esc(family),
            year=esc(fmt_year(match.get("model_year"))),
            narrative=esc(clip_words(match.get("narrative"))),
            complaint_id=esc(match.get("complaint_id")),
        ),
        unsafe_allow_html=True,
    )


def render_year_chart(results, vehicle):
    """Complaints by year for the component that dominates these results.

    Built from what came back, so it describes the result set rather than the
    corpus. With five results it is a shape, not a statistic, and it is labelled
    that way.
    """
    components = [m.get("component") for m in results if m.get("component")]
    if not components:
        return
    top_component = Counter(components).most_common(1)[0][0]

    years = Counter(
        fmt_year(m.get("model_year"))
        for m in results
        if m.get("component") == top_component and fmt_year(m.get("model_year"))
    )
    if not years:
        return

    ordered = sorted(years)
    scope = (vehicle or {}).get("model_family") if vehicle else None
    eyebrow(
        "{} complaints by model year{}".format(
            sentence_case(top_component), ", {}".format(scope) if scope else ""
        )
    )
    st.bar_chart(
        {"Model year": ordered, "Complaints": [years[y] for y in ordered]},
        x="Model year",
        y="Complaints",
        color=ACCENT,
        height=220,
    )
    st.caption("From the {} results above, not the whole corpus.".format(len(results)))


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
            "Searching without one returns complaints from every vehicle on record."
        )

    symptom = st.text_area(
        "Symptom",
        value=st.session_state.symptom,
        height=120,
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
            # Skeletons stream to the browser before the blocking call, then are
            # cleared once the real cards are ready to render below.
            placeholder = st.empty()
            with placeholder.container():
                rule()
                render_skeletons(NUM_RESULTS)
            ok, matches = attempt(
                "Search",
                cached_search,
                symptom=symptom.strip(),
                model_family=scope_family,
                scoped=bool(scope_family),
            )
            placeholder.empty()
            if ok:
                st.session_state.results = matches or []
                st.session_state.results_symptom = symptom.strip()
                st.session_state.results_scoped = bool(scope_family)
                st.session_state.agent_answer = None
                st.session_state.searched = True

    results = st.session_state.results
    if not results:
        if st.session_state.searched:
            rule()
            empty_state(
                NO_RESULTS_SVG,
                "No complaints matched",
                "Nothing in the indexed records came back for that description. "
                "Try fewer words, or describe what the car does rather than the part.",
            )
        return

    rule()
    eyebrow(
        "Matching complaints, scoped to {}".format(
            (vehicle or {}).get("model_family", "")
        )
        if st.session_state.results_scoped
        else "Matching complaints, all vehicles"
    )
    for match in results:
        render_complaint_card(match, vehicle, st.session_state.results_scoped)

    render_year_chart(results, vehicle if st.session_state.results_scoped else None)

    # An unscoped search returns other vehicles, so saving it would attach
    # complaints about someone else's car to this one.
    saveable = vehicle is not None and st.session_state.results_scoped
    save_column, note_column = st.columns([1, 3])
    with save_column:
        save = st.button(
            "Save to vehicle",
            disabled=not saveable,
            use_container_width=True,
        )
    with note_column:
        if vehicle is None:
            st.caption("Select a vehicle in Garage to save this report.")
        elif not st.session_state.results_scoped:
            st.caption(
                "These results are not about your vehicle, so they cannot be saved "
                "against it. Turn on Scope to my vehicle and search again."
            )
        else:
            st.caption(
                "Saves the symptom and these {} complaints against {}.".format(
                    len(results), vehicle_title(vehicle)
                )
            )

    if save and saveable:
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
        # Rendered straight onto the page. A bordered container here would be
        # another surface, and the answer is markdown so it cannot be wrapped
        # in our own div without losing the formatting.
        st.markdown(st.session_state.agent_answer)


# ----------------------------------------------------------------- insights

# Every tab body renders on every rerun, so without a cache these two warehouse
# queries would run each time anything on any tab is clicked. Exceptions are not
# cached by st.cache_data, so a failed read still surfaces through attempt().

@st.cache_data(ttl=60, show_spinner=False)
def cached_tool_usage():
    return vsf.tool_usage()


@st.cache_data(ttl=60, show_spinner=False)
def cached_recent_events(limit):
    return vsf.recent_events(limit=limit)


def render_insights():
    st.markdown("# Insights")
    lede(
        "Every tool call and user action appends a row to workspace.vsf.app_events, "
        "a Delta table with change data feed enabled. These numbers are read back "
        "from that log, so the app reports on itself."
    )

    freshness, refresh = st.columns([3, 1])
    with freshness:
        st.caption("Read from the event log, cached for up to 60 seconds.")
    with refresh:
        if st.button("Refresh", key="refresh_insights", use_container_width=True):
            cached_tool_usage.clear()
            cached_recent_events.clear()
            st.rerun()

    rule()
    eyebrow("Tool usage")
    with st.spinner("Reading the event log"):
        ok, usage = attempt("Tool usage", cached_tool_usage)

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
        ok, events = attempt("Recent events", cached_recent_events, limit=25)

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
        "vehicles": None,
        "selected_vehicle_id": None,
        "symptom": "",
        "scoped": True,
        "results": [],
        "results_symptom": "",
        "results_scoped": True,
        "searched": False,
        "agent_answer": None,
        "warmed": False,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def render_header():
    vehicle = selected_vehicle()
    if vehicle:
        chip = "<div class='vsf-chip'>{}</div>".format(esc(vehicle_title(vehicle)))
    else:
        chip = "<div class='vsf-chip is-empty'>No vehicle selected</div>"
    st.markdown(
        "<div class='vsf-header'>"
        "<div class='vsf-brand'>Vehicle Safety Signal Finder</div>"
        "{}</div>".format(chip),
        unsafe_allow_html=True,
    )


def main():
    st.markdown(CSS, unsafe_allow_html=True)

    if IMPORT_ERROR:
        st.error("The shared tool module failed to load, so no data is available.")
        st.code(IMPORT_ERROR, language="text")
        return

    init_state()
    warm_warehouse()
    # Loaded before the header so the vehicle chip is right on the first render.
    load_vehicles()
    render_header()

    garage_tab, ask_tab, insights_tab = st.tabs(SCREENS)
    with garage_tab:
        render_garage()
    with ask_tab:
        render_ask()
    with insights_tab:
        render_insights()

    st.markdown(
        "<div class='vsf-footer'>{}</div>".format(esc(DISCLAIMER)),
        unsafe_allow_html=True,
    )


main()
