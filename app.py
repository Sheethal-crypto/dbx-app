import streamlit as st
from lib.db import query, execute, SCHEMA
from lib import style

st.set_page_config(page_title="Support Desk", layout="wide")
style.inject()

STATUSES = ["open", "in_progress", "resolved"]
PRIORITIES = ["low", "medium", "high"]


def load_tickets(status_filter):
    where = "WHERE t.status = %s" if status_filter != "all" else ""
    sql = f"""
        SELECT t.ticket_id, t.title, t.status, t.priority,
               t.created_by, t.created_at,
               COUNT(m.message_id) AS message_count
        FROM {SCHEMA}.tickets t
        LEFT JOIN {SCHEMA}.ticket_messages m ON m.ticket_id = t.ticket_id
        {where}
        GROUP BY t.ticket_id
        ORDER BY t.created_at DESC
    """
    return query(sql, (status_filter,) if status_filter != "all" else ())


st.title("Support Desk")

try:
    stats = query(f"SELECT status, COUNT(*) AS n FROM {SCHEMA}.tickets GROUP BY status")
except Exception as exc:
    st.error("Cannot reach the database. Check the app's database resource and grants.")
    st.exception(exc)
    st.stop()

counts = {r["status"]: r["n"] for r in stats}
style.tiles(sum(counts.values()), counts, STATUSES)

left, right = st.columns([3, 2], gap="large")

with left:
    st.subheader("Board")
    chosen = st.selectbox("Filter by status", ["all"] + STATUSES)
    tickets = load_tickets(chosen)

    if not tickets:
        style.empty("No tickets with this status. Create one to get started.")
    else:
        style.board(tickets)

    ids = [t["ticket_id"] for t in tickets]
    selected = st.selectbox("Open ticket", ids) if ids else None

    if selected:
        ticket = next(t for t in tickets if t["ticket_id"] == selected)
        style.detail_header(ticket)

        new_status = st.selectbox(
            "Status", STATUSES, index=STATUSES.index(ticket["status"]), key="status_sel"
        )
        if st.button("Update status"):
            if new_status == ticket["status"]:
                st.warning(f"Already {new_status.replace('_', ' ')}.")
            else:
                execute(
                    f"UPDATE {SCHEMA}.tickets SET status = %s WHERE ticket_id = %s",
                    (new_status, selected),
                )
                st.success(f"Status updated to {new_status.replace('_', ' ')}.")
                st.rerun()

        st.subheader("Thread")
        msg_rows = query(
            f"""SELECT author, message_text, created_at
                FROM {SCHEMA}.ticket_messages
                WHERE ticket_id = %s ORDER BY created_at""",
            (selected,),
        )
        if msg_rows:
            style.messages(msg_rows)
        else:
            style.empty("No messages yet. Add the first one below.")

        with st.form("add_message", clear_on_submit=True):
            author = st.text_input("Your name")
            body = st.text_area("Message")
            if st.form_submit_button("Add message"):
                if not author.strip() or not body.strip():
                    st.error("Enter both your name and a message.")
                else:
                    execute(
                        f"""INSERT INTO {SCHEMA}.ticket_messages
                            (ticket_id, message_text, author) VALUES (%s, %s, %s)""",
                        (selected, body.strip(), author.strip()),
                    )
                    st.success("Message added.")
                    st.rerun()

with right:
    st.subheader("New ticket")
    with st.form("new_ticket", clear_on_submit=True):
        title = st.text_input("Title")
        created_by = st.text_input("Created by")
        priority = st.selectbox("Priority", PRIORITIES, index=1)
        first_message = st.text_area("Describe the issue")

        if st.form_submit_button("Create ticket"):
            if len(title.strip()) < 5:
                st.error("Title must be at least 5 characters.")
            elif not created_by.strip():
                st.error("Enter who is opening this ticket.")
            else:
                rows = query(
                    f"""INSERT INTO {SCHEMA}.tickets
                        (title, status, priority, created_by)
                        VALUES (%s, 'open', %s, %s) RETURNING ticket_id""",
                    (title.strip(), priority, created_by.strip()),
                )
                new_id = rows[0]["ticket_id"]
                if first_message.strip():
                    execute(
                        f"""INSERT INTO {SCHEMA}.ticket_messages
                            (ticket_id, message_text, author) VALUES (%s, %s, %s)""",
                        (new_id, first_message.strip(), created_by.strip()),
                    )
                st.success(f"Created ticket #{new_id:04d}.")
                st.rerun()
