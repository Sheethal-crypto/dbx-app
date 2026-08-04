from html import escape
import streamlit as st

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

:root {
  --ground: #EDEFEA;
  --panel: #F7F8F5;
  --field: #FFFFFF;
  --ink: #17201C;
  --ink-soft: #5A655E;
  --rule: #CBD2C8;
  --open: #C07A12;
  --in_progress: #1F5F8B;
  --resolved: #4C6B3C;
}

html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }

h1, h2, h3 {
  font-family: 'Barlow Condensed', sans-serif !important;
  letter-spacing: 0.01em;
}

h1 {
  font-weight: 700 !important;
  text-transform: uppercase;
  font-size: 2.6rem !important;
  border-bottom: 2px solid var(--ink);
  padding-bottom: 0.2rem;
}

.eyebrow {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.7rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ink-soft);
}

/* Status tiles */
.tiles { display: flex; gap: 0.6rem; margin: 0.4rem 0 1.4rem 0; flex-wrap: wrap; }
.tile {
  flex: 1 1 120px;
  background: var(--panel);
  border: 1px solid var(--rule);
  border-top: 3px solid var(--ink);
  padding: 0.7rem 0.9rem;
}
.tile.open { border-top-color: var(--open); }
.tile.in_progress { border-top-color: var(--in_progress); }
.tile.resolved { border-top-color: var(--resolved); }
.tile .n {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 2.4rem; font-weight: 700; line-height: 1; color: var(--ink);
}

/* Board rows */
.row {
  display: grid;
  grid-template-columns: 4px 5.5rem 1fr auto;
  gap: 0.9rem;
  align-items: center;
  background: var(--panel);
  border: 1px solid var(--rule);
  border-left: none;
  margin-bottom: 0.35rem;
  padding: 0.55rem 0.9rem 0.55rem 0;
}
.row .bar { background: var(--rule); align-self: stretch; }
.row.open .bar { background: var(--open); }
.row.in_progress .bar { background: var(--in_progress); }
.row.resolved .bar { background: var(--resolved); }
.row .id {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.8rem; color: var(--ink-soft); padding-left: 0.9rem;
}
.row .title { font-weight: 500; }
.row .meta {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.7rem; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--ink-soft); white-space: nowrap;
}

/* Ticket detail header */
.detail {
  background: var(--panel);
  border: 1px solid var(--rule);
  border-top: 3px solid var(--ink);
  padding: 0.9rem 1.1rem;
  margin-bottom: 1rem;
}
.detail.open { border-top-color: var(--open); }
.detail.in_progress { border-top-color: var(--in_progress); }
.detail.resolved { border-top-color: var(--resolved); }
.detail h2 {
  margin: 0.15rem 0 0.3rem 0 !important;
  font-size: 1.6rem !important;
  font-weight: 600 !important;
}

/* Message thread */
.msg {
  border-left: 2px solid var(--rule);
  padding: 0.1rem 0 0.5rem 0.85rem;
  margin-bottom: 0.9rem;
}
.msg .who {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.72rem; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--ink-soft);
}
.msg .body { margin-top: 0.2rem; }

.empty {
  border: 1px dashed var(--rule);
  padding: 1.2rem;
  color: var(--ink-soft);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.8rem;
}

/* Forms */
div[data-testid="stForm"] {
  background: var(--panel);
  border: 1px solid var(--rule);
  border-radius: 0;
}

.stButton button, div[data-testid="stForm"] button {
  border-radius: 0 !important;
  font-family: 'Barlow Condensed', sans-serif !important;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 600 !important;
  cursor: pointer !important;
}

/* Inputs: visible at rest, not only on focus */
div[data-baseweb="input"],
div[data-baseweb="textarea"],
div[data-baseweb="select"] > div {
  border: 1px solid var(--rule) !important;
  border-radius: 0 !important;
  background: var(--field) !important;
  transition: border-color 120ms ease;
}

div[data-baseweb="input"]:hover,
div[data-baseweb="textarea"]:hover,
div[data-baseweb="select"] > div:hover {
  border-color: var(--ink-soft) !important;
}

div[data-baseweb="input"]:focus-within,
div[data-baseweb="textarea"]:focus-within,
div[data-baseweb="select"] > div:focus-within {
  border-color: var(--in_progress) !important;
  box-shadow: 0 0 0 2px rgba(31, 95, 139, 0.18) !important;
}

div[data-baseweb="select"], div[data-baseweb="select"] * { cursor: pointer !important; }
li[role="option"] { cursor: pointer !important; }

label[data-testid="stWidgetLabel"] p {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.72rem !important;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ink-soft);
}

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; }
}
</style>
"""


def inject():
    st.markdown(CSS, unsafe_allow_html=True)


def tiles(total, counts, statuses):
    html = (
        '<div class="tiles"><div class="tile">'
        f'<div class="eyebrow">All tickets</div><div class="n">{total}</div></div>'
    )
    for s in statuses:
        html += (
            f'<div class="tile {s}"><div class="eyebrow">{s.replace("_", " ")}</div>'
            f'<div class="n">{counts.get(s, 0)}</div></div>'
        )
    st.markdown(html + "</div>", unsafe_allow_html=True)


def board(tickets):
    html = ""
    for t in tickets:
        html += (
            f'<div class="row {t["status"]}"><div class="bar"></div>'
            f'<div class="id">#{t["ticket_id"]:04d}</div>'
            f'<div class="title">{escape(t["title"])}</div>'
            f'<div class="meta">{t["priority"]} &nbsp;·&nbsp; '
            f'{t["message_count"]} msg &nbsp;·&nbsp; '
            f'{escape(t["created_by"])}</div></div>'
        )
    st.markdown(html, unsafe_allow_html=True)


def detail_header(ticket):
    st.markdown(
        f'<div class="detail {ticket["status"]}">'
        f'<div class="eyebrow">Ticket #{ticket["ticket_id"]:04d} &nbsp;·&nbsp; '
        f'{ticket["priority"]} priority &nbsp;·&nbsp; '
        f'opened {ticket["created_at"]:%d %b %Y} by '
        f'{escape(ticket["created_by"])}</div>'
        f'<h2>{escape(ticket["title"])}</h2></div>',
        unsafe_allow_html=True,
    )


def messages(rows):
    html = ""
    for m in rows:
        html += (
            f'<div class="msg"><div class="who">{escape(m["author"])} &nbsp;·&nbsp; '
            f'{m["created_at"]:%d %b %H:%M}</div>'
            f'<div class="body">{escape(m["message_text"])}</div></div>'
        )
    st.markdown(html, unsafe_allow_html=True)


def empty(text):
    st.markdown(f'<div class="empty">{text}</div>', unsafe_allow_html=True)
