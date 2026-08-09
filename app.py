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
  /* Hover goes lighter, not darker. Darkening the fill dropped white label
     contrast to the point the button was hard to read. */
  --accent-light: #158577;
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

h1 { font-size: 44px; font-weight: 700; letter-spacing: -0.024em; color: var(--ink);
     margin: 0 0 10px; line-height: 1.15; }
h2 { font-size: 21px; font-weight: 650; color: var(--ink); }
h3 { font-size: 18px; font-weight: 650; color: var(--ink); }
p, li, label, .stMarkdown { font-size: 18px; }

/* header, with a low opacity teal wash bleeding out to the page edges */
.vsf-header {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; flex-wrap: wrap;
  margin: 0 -1.25rem; padding: 14px 1.25rem 16px;
  background: linear-gradient(180deg, rgba(15, 118, 110, 0.07) 0%,
                                      rgba(15, 118, 110, 0.02) 55%,
                                      rgba(15, 118, 110, 0) 100%);
}
.vsf-brand { font-size: 30px; font-weight: 700; letter-spacing: -0.02em; color: var(--ink); }
.vsf-chip {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--accent); color: #FFFFFF; border-radius: 999px;
  padding: 9px 18px; font-size: 15px; font-weight: 600; letter-spacing: 0.01em;
  box-shadow: var(--lift);
}
.vsf-chip.is-empty { background: var(--card); color: var(--ink-soft); font-weight: 500; }
.vsf-chip.is-fresh { animation: vsf-pulse 620ms ease-out 1; }
@keyframes vsf-pulse {
  0%   { transform: scale(1);    box-shadow: 0 0 0 0 rgba(15, 118, 110, 0.45); }
  45%  { transform: scale(1.045); box-shadow: 0 0 0 10px rgba(15, 118, 110, 0); }
  100% { transform: scale(1);    box-shadow: 0 0 0 0 rgba(15, 118, 110, 0); }
}

/* Indeterminate bar under the header. Shown only while Streamlit's own running
   indicator is in the DOM, which is exactly the duration of any blocking call.
   If that testid ever disappears the bar simply stays hidden. */
.vsf-progress {
  height: 3px; border-radius: 2px; overflow: hidden; background: rgba(15, 118, 110, 0.1);
  opacity: 0; transition: opacity 120ms ease; margin: 0 0 10px;
}
.stApp:has([data-testid="stStatusWidget"]) .vsf-progress { opacity: 1; }
.vsf-progress::after {
  content: ""; display: block; height: 100%; width: 34%; border-radius: 2px;
  background: var(--accent); animation: vsf-indeterminate 1.1s ease-in-out infinite;
}
@keyframes vsf-indeterminate {
  0%   { transform: translateX(-100%); }
  100% { transform: translateX(330%); }
}

/* tabs, sized as primary navigation rather than a secondary control */
.stTabs [data-baseweb="tab-list"] {
  gap: 12px; background: transparent; border-bottom: 1px solid var(--hairline);
  margin-bottom: 16px;
}
.stTabs [data-baseweb="tab"] {
  height: 52px; padding: 0 18px; background: transparent;
  font-size: 18px; font-weight: 550; color: var(--ink-soft);
}
.stTabs [data-baseweb="tab"][aria-selected="true"] { color: var(--ink); font-weight: 650; }
.stTabs [data-baseweb="tab-highlight"] { background-color: var(--accent); height: 3px; }
.stTabs [data-baseweb="tab-border"] { display: none; }
.stTabs [data-baseweb="tab-panel"] { padding-top: 4px; }

/* text blocks */
/* Section labels. At body size they need the primary ink to still read as
   labels rather than as another line of grey copy. */
.vsf-eyebrow {
  font-size: 19px; font-weight: 600; letter-spacing: -0.005em;
  color: var(--ink); margin: 0 0 12px;
}
.vsf-lede { font-size: 18px; line-height: 1.6; color: var(--ink-soft); margin: 0 0 20px; }
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
.vsf-component { font-size: 20px; font-weight: 650; color: var(--ink); }
.vsf-year { font-size: 15px; color: var(--ink-soft); white-space: nowrap; }
/* The narrative is the content of the card, so it takes the primary colour.
   All caps at low contrast is harder to read than the size alone suggests. */
.vsf-narrative {
  font-size: 17px; line-height: 1.7; letter-spacing: 0.015em;
  color: var(--ink); margin: 14px 0 16px;
}
.vsf-complaint-id { font-family: var(--mono); font-size: 14px; color: var(--ink-soft); }
.vsf-badge {
  display: inline-block; font-size: 14px; font-weight: 600; color: var(--amber);
  background: var(--amber-wash); border-radius: 999px; padding: 4px 12px; margin-bottom: 12px;
}

/* synthesis card. Tinted and ringed rather than white and floating, so it reads
   as commentary on the complaints below it rather than another complaint. */
.vsf-synthesis {
  background: #F1F6F5; border-radius: 14px; padding: 24px 26px; margin-bottom: 16px;
  box-shadow: inset 0 0 0 1px rgba(15, 118, 110, 0.2);
  animation: vsf-rise 220ms ease both;
}
.vsf-synthesis-title {
  font-size: 18px; font-weight: 700; color: var(--accent); margin-bottom: 16px;
}
.vsf-syn-row { margin-bottom: 14px; }
.vsf-syn-row:last-child { margin-bottom: 0; }
.vsf-syn-label { font-size: 14px; font-weight: 650; color: var(--accent); margin-bottom: 3px; }
.vsf-syn-body { font-size: 16px; line-height: 1.6; color: var(--ink); }
.vsf-syn-body.mono { font-family: var(--mono); font-size: 14px; color: var(--ink-soft); }
.vsf-synthesis.is-muted { background: var(--card); box-shadow: var(--lift); }
.vsf-synthesis.is-muted .vsf-syn-body { color: var(--ink-soft); }
.vsf-syn-sk {
  height: 12px; border-radius: 6px; margin-bottom: 14px;
  background: linear-gradient(90deg, #E2ECEA 0%, #EFF5F4 50%, #E2ECEA 100%);
  background-size: 200% 100%;
  animation: vsf-shimmer 1.4s ease-in-out infinite;
}
.vsf-syn-sk.w30 { width: 30%; }
.vsf-syn-sk.w85 { width: 85%; }
.vsf-syn-sk.w70 { width: 70%; }
.vsf-syn-sk.w45 { width: 45%; margin-bottom: 0; }

/* Sources. Its own block below the chart, deliberately not a card: it is a
   citation line, not another surface. */
.vsf-sources { margin: 0 0 20px; }
.vsf-sources-label {
  font-size: 15px; font-weight: 600; color: var(--ink-soft); margin-bottom: 6px;
}
.vsf-sources-ids {
  font-family: var(--mono); font-size: 15px; line-height: 1.65; color: var(--ink);
  word-break: break-word;
}
.vsf-sources-sk {
  height: 12px; width: 52%; border-radius: 6px;
  background: linear-gradient(90deg, var(--skeleton) 0%, var(--skeleton-sheen) 50%,
                                     var(--skeleton) 100%);
  background-size: 200% 100%;
  animation: vsf-shimmer 1.4s ease-in-out infinite;
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
  background: var(--accent-light); border-color: var(--accent-light); color: #FFFFFF;
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

/* scope toggle. st.toggle renders as a checkbox widget, so the switch is scaled
   from its left edge and the row is given room to absorb the extra width. */
[data-testid="stCheckbox"] label { align-items: center; gap: 16px; }
[data-testid="stCheckbox"] label > div:first-child {
  transform: scale(1.3); transform-origin: left center; margin-right: 12px;
}
[data-testid="stCheckbox"] label p,
[data-testid="stCheckbox"] [data-testid="stWidgetLabel"] p { font-size: 17px; }

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

/* count-up on the Insights figures. --target is set inline per cell. */
@property --vsf-num {
  syntax: "<integer>";
  initial-value: 0;
  inherits: false;
}
.vsf-count { counter-reset: vsf-n var(--vsf-num); animation: vsf-count 600ms ease-out forwards; }
.vsf-count::after { content: counter(vsf-n); }
@keyframes vsf-count { from { --vsf-num: 0; } to { --vsf-num: var(--target); } }

/* Both button flavours, since Add vehicle is a form submit and Search is not. */
.stButton > button[kind="primary"]:hover,
.stFormSubmitButton > button[kind="primary"]:hover {
  box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.16), 0 6px 16px rgba(15, 118, 110, 0.22);
}
.stButton > button[kind="primary"]:active,
.stFormSubmitButton > button[kind="primary"]:active {
  background: var(--accent); border-color: var(--accent);
  transform: translateY(1px); box-shadow: none;
}

/* One switch for everything above. */
@media (prefers-reduced-motion: reduce) {
  .vsf-card, .vsf-empty, .vsf-synthesis { animation: none !important; }
  .vsf-card { transition: none !important; }
  .vsf-card:hover { transform: none !important; }
  .vsf-chip.is-fresh { animation: none !important; }
  .vsf-sk-line, .vsf-syn-sk { animation: none !important; }
  .vsf-progress::after { animation: none !important; width: 100%; }
  .stButton > button[kind="primary"]:active { transform: none !important; }
  /* Land on the real figure instead of counting to it. */
  .vsf-count { animation: none !important; --vsf-num: var(--target); }
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


def count_up(value):
    """A figure that counts to itself over 600ms.

    Uses a registered custom property, so the browser must support @property.
    Anything not parseable as a number is rendered as plain text instead.
    """
    try:
        target = int(float(value))
    except (TypeError, ValueError):
        return esc(value)
    return "<span class='vsf-count' style='--target: {}'></span>".format(target)


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
    load_vehicles()

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
            if ok and decoded.get("status") != "ok":
                # invalid or not_found: there is no vehicle to save.
                st.warning(decoded.get("message") or "That VIN could not be decoded.")
            elif ok:
                with preview.container():
                    render_vehicle_card(decoded, is_selected=False, pending=True)
                with st.spinner("Saving to your garage"):
                    saved_ok, added = attempt("Save vehicle", vsf.save_vehicle, decoded=decoded)
                preview.empty()
                if saved_ok:
                    # added and exists are both successes: either way the user now has
                    # exactly one row for this VIN, and it is the one we select.
                    load_vehicles(force=True)
                    st.session_state.selected_vehicle_id = added.get("vehicle_id")
                    st.success("{} {}. Selected for search.".format(
                        "Already in your garage:" if added.get("status") == "exists" else "Added",
                        vehicle_title(added),
                    ))

    rule()

    # Re-read: a successful add refreshed the list above.
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

def render_complaint_card(match, vehicle, scoped, delay_ms=0):
    """One complaint. Marked as a different vehicle only when the search was
    unscoped, since a scoped search cannot return another family."""
    family = (match.get("model_family") or "").upper()
    wanted = ((vehicle or {}).get("model_family") or "").upper()
    is_other = bool(not scoped and wanted and family and family != wanted)

    badge = ""
    if is_other:
        badge = "<div class='vsf-badge'>Different vehicle &middot; {}</div>".format(esc(family))

    st.markdown(
        "<div class='vsf-card {other}' style='animation-delay: {delay}ms'>"
        "{badge}"
        "<div class='vsf-head'>"
        "<div class='vsf-component'>{component}</div>"
        "<div class='vsf-year'>{family} {year}</div>"
        "</div>"
        "<div class='vsf-narrative'>{narrative}</div>"
        "<div class='vsf-complaint-id'>Complaint {complaint_id}</div>"
        "</div>".format(
            other="is-other" if is_other else "",
            delay=delay_ms,
            badge=badge,
            component=esc(sentence_case(match.get("component")) or "Component not recorded"),
            family=esc(family),
            year=esc(fmt_year(match.get("model_year"))),
            narrative=esc(clip_words(match.get("narrative"))),
            complaint_id=esc(match.get("complaint_id")),
        ),
        unsafe_allow_html=True,
    )


SYNTHESIS_SECTIONS = ["Pattern", "Recall status", "Terms for your mechanic", "Sources"]
# Sources is still asked for and still parsed, it just renders as its own block
# below the chart rather than as a fourth row inside the card.
CARD_SECTIONS = ["Pattern", "Recall status", "Terms for your mechanic"]


def synthesis_prompt(symptom, vehicle, results):
    """Ask for four labelled plain-text lines.

    The returned complaints are handed over in the prompt rather than left for
    the agent to re-search, so Pattern and Sources describe the set actually on
    screen and the agent only needs its recall tool.
    """
    listed = "\n".join(
        "- {} | {} | {}".format(
            m.get("complaint_id"), m.get("component"), fmt_year(m.get("model_year"))
        )
        for m in results
    )
    return (
        "My {title} (make {make}, model family {family}, year {year}).\n"
        "I described the problem as: \"{symptom}\"\n\n"
        "These are the complaints the app returned, all for this vehicle. They are "
        "listed below your summary in the app, so do not refer to them as being "
        "above it:\n"
        "{listed}\n\n"
        "Write exactly four lines of plain text. No markdown, no bullet characters. "
        "Each line starts with the label and a colon:\n\n"
        "Pattern: one sentence on whether this looks like a widely reported issue for "
        "this vehicle, and how many of the returned complaints share a component.\n"
        "Recall status: one sentence. Check NHTSA for an open campaign covering that "
        "component. Give the campaign number if there is one, otherwise say plainly "
        "that there is none.\n"
        "Terms for your mechanic: two or three terms, comma separated, the engineering "
        "vocabulary NHTSA uses for what I described, so I can name it at the service "
        "desk.\n"
        "Sources: the complaint ids you drew on, comma separated.\n\n"
        "Do not suggest causes, repairs, parts or urgency. Report only what the "
        "records contain."
    ).format(
        title=vehicle_title(vehicle),
        make=vehicle.get("make"),
        family=vehicle.get("model_family"),
        year=fmt_year(vehicle.get("model_year")),
        symptom=symptom,
        listed=listed,
    )


def parse_sections(text):
    """Pull the four labelled lines out of the answer.

    Models drift into markdown, so leading bullets and asterisks are stripped and
    a line that is not a label is treated as a continuation of the last one.
    """
    found, current = {}, None
    for line in (text or "").splitlines():
        cleaned = line.strip().lstrip("-*# ").replace("**", "").strip()
        if not cleaned:
            continue
        label = next(
            (s for s in SYNTHESIS_SECTIONS if cleaned.lower().startswith(s.lower() + ":")),
            None,
        )
        if label:
            current = label
            found[label] = cleaned[len(label) + 1:].strip()
        elif current:
            found[current] = (found[current] + " " + cleaned).strip()
    return found


@st.cache_data(ttl=900, show_spinner=False)
def cached_synthesis(symptom, model_family, _prompt):
    """Keyed on (symptom, model_family) only.

    _prompt is excluded from the key by Streamlit's leading underscore rule: it
    is derived from those two values, and this is the slowest call in the app.
    """
    return vsf.run_agent(user_message=_prompt)


def synthesis_card(body, muted=False):
    return (
        "<div class='vsf-synthesis{muted}'>"
        "<div class='vsf-synthesis-title'>What this means</div>{body}</div>"
    ).format(muted=" is-muted" if muted else "", body=body)


def reserve_synthesis(scoped):
    """Claim the slot above the complaints and fill it with a placeholder.

    Returns the slot, or None when there is nothing to run. The card sits above
    the evidence, but it is the slowest thing on the page, so the slot is only
    reserved here and written to once the complaints are already on screen.
    """
    slot = st.empty()
    if not scoped:
        slot.markdown(
            synthesis_card(
                "<div class='vsf-syn-body'>The summary needs a vehicle scope. Turn on "
                "Scope to my vehicle and search again to read these complaints against "
                "your car.</div>",
                muted=True,
            ),
            unsafe_allow_html=True,
        )
        return None

    slot.markdown(
        synthesis_card(
            "<div class='vsf-syn-sk w30'></div><div class='vsf-syn-sk w85'></div>"
            "<div class='vsf-syn-sk w70'></div><div class='vsf-syn-sk w45'></div>"
        ),
        unsafe_allow_html=True,
    )
    return slot


def reserve_sources():
    """Claim the Sources slot under the chart and hold its height.

    Reserved rather than left blank so the complaints below do not jump down
    when the citation line arrives.
    """
    slot = st.empty()
    slot.markdown(
        "<div class='vsf-sources'><div class='vsf-sources-label'>Sources</div>"
        "<div class='vsf-sources-sk'></div></div>",
        unsafe_allow_html=True,
    )
    return slot


def fill_synthesis(card_slot, sources_slot, results, vehicle):
    """Run the agent and write into the two reserved slots.

    The call is made outside either slot so both skeletons stay visible while it
    runs, and a failure is rendered into the card slot rather than at the foot of
    the page, which is where attempt() would put it.
    """
    try:
        answer = cached_synthesis(
            symptom=st.session_state.results_symptom,
            model_family=vehicle.get("model_family"),
            _prompt=synthesis_prompt(st.session_state.results_symptom, vehicle, results),
        )
    except Exception as exc:  # noqa: BLE001 - the complaints below stand on their own
        detail = traceback.format_exc()
        sources_slot.empty()
        with card_slot.container():
            st.error("Summary failed: {}".format(exc))
            with st.expander("Error detail"):
                st.code(detail, language="text")
        return

    if not answer:
        card_slot.empty()
        sources_slot.empty()
        return

    sections = parse_sections(answer)
    if sections:
        body = "".join(
            "<div class='vsf-syn-row'><div class='vsf-syn-label'>{}</div>"
            "<div class='vsf-syn-body'>{}</div></div>".format(
                esc(label), esc(sections[label])
            )
            for label in CARD_SECTIONS
            if sections.get(label)
        )
    else:
        # The model ignored the format. Show what it said rather than nothing.
        body = "<div class='vsf-syn-body'>{}</div>".format(esc(answer))

    card_slot.markdown(synthesis_card(body), unsafe_allow_html=True)

    ids = sections.get("Sources") if sections else None
    if ids:
        sources_slot.markdown(
            "<div class='vsf-sources'><div class='vsf-sources-label'>Sources</div>"
            "<div class='vsf-sources-ids'>{}</div></div>".format(esc(ids)),
            unsafe_allow_html=True,
        )
    else:
        sources_slot.empty()


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
    st.caption(
        "From the {} matching complaints, not all complaints on record.".format(
            len(results)
        )
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
    # Everything above the complaints is claimed first, in final page order:
    # the summary card, then the chart, then the Sources line. Only the two
    # agent-dependent blocks are slots. The chart is built from results already
    # in hand, so it paints here rather than waiting with them.
    scoped = st.session_state.results_scoped
    card_slot = reserve_synthesis(scoped and vehicle is not None)
    render_year_chart(results, vehicle if scoped else None)
    sources_slot = reserve_sources() if card_slot is not None else None

    eyebrow(
        "Matching complaints, scoped to {}".format(
            (vehicle or {}).get("model_family", "")
        )
        if scoped
        else "Matching complaints, all vehicles"
    )
    # 60ms apart so five results cascade rather than landing at once.
    for position, match in enumerate(results):
        render_complaint_card(match, vehicle, scoped, delay_ms=position * 60)

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

    # Last, written back into the two slots above the complaints. Everything the
    # user can read or click is already painted before this blocks.
    if card_slot is not None:
        fill_synthesis(card_slot, sources_slot, results, vehicle)


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
                    (count_up(calls), "num"),
                    (count_up(p50), "num"),
                    (count_up(p95), "num"),
                    (
                        "<span class='{}'>{}</span>".format(
                            "zero" if failed == "0" else "fail", count_up(failures)
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
                    (count_up(latency_ms), "num"),
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
        "warmed": False,
        "chip_vehicle": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def render_header():
    vehicle = selected_vehicle()
    vehicle_id = vehicle["vehicle_id"] if vehicle else None
    # Pulse once, on the render where the selection actually changed.
    fresh = " is-fresh" if vehicle and vehicle_id != st.session_state.chip_vehicle else ""
    st.session_state.chip_vehicle = vehicle_id

    if vehicle:
        chip = "<div class='vsf-chip{}'>{}</div>".format(fresh, esc(vehicle_title(vehicle)))
    else:
        chip = "<div class='vsf-chip is-empty'>No vehicle selected</div>"
    st.markdown(
        "<div class='vsf-header'>"
        "<div class='vsf-brand'>Vehicle Safety Signal Finder</div>"
        "{}</div>"
        "<div class='vsf-progress'></div>".format(chip),
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
