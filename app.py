import streamlit as st
from lib.db import query, execute, SCHEMA

st.set_page_config(page_title="Support Desk", layout="wide")

STATUSES = ["open", "in_progress", "resolved"]
PRIORITIES = ["low", "medium", "high"]


def load_tickets(status_filter):
    sql = f"""
        SELECT t.ticket_id, t.title, t.status, t.priority,
               t.created_by, t.created_at,
               COUNT(m.message_id) AS message_count
        FROM {SCHEMA}.tickets t
        LEFT JOIN {SCHEMA}.ticket_messages m ON m.ticket_id = t.ticket_id
        {"WHERE t.status = %s" if status_filter != "all" else ""}
        GROUP BY t.ticket_id
        ORDER BY t.created_at DESC
    """
    return query(sql, (status_filter,) if status_filter != "all" else ())


st.title("Support Desk")

try:
    stats = query(f"""
        SELECT status, COUNT(*) AS n FROM {SCHEMA}.tickets GROUP BY status
    """)
except Exception as exc:
    st.error("Could not reach the database.")
    st.exception(exc)
    st.stop()

counts = {r["status"]: r["n"] for r in stats}
cols = st.columns(4)
cols[0].metric("Total", sum(counts.values()))
for col, s in zip(cols[1:], STATUSES):
    col.metric(s.replace("_", " ").title(), counts.get(s, 0))

st.divider()
left, right = st.columns([3, 2])

with left:
    st.subheader("Tickets")
    chosen = st.selectbox("Filter by status", ["all"] + STATUSES)
    tickets = load_tickets(chosen)

    if not tickets:
        st.info("No tickets match this filter.")
    else:
        st.dataframe(tickets, use_container_width=True, hide_index=True)

    ids = [t["ticket_id"] for t in tickets]
    selected = st.selectbox("Open ticket", ids) if ids else None

    if selected:
        ticket = next(t for t in tickets if t["ticket_id"] == selected)
        st.markdown(f"### #{ticket['ticket_id']} {ticket['title']}")
        st.caption(
            f"{ticket['status']} | {ticket['priority']} | "
            f"opened by {ticket['created_by']}"
        )

        new_status = st.selectbox(
            "Status", STATUSES, index=STATUSES.index(ticket["status"]), key="status_sel"
        )
        if st.button("Update status"):
            if new_status == ticket["status"]:
                st.warning("Status unchanged.")
            else:
                execute(
                    f"UPDATE {SCHEMA}.tickets SET status = %s WHERE ticket_id = %s",
                    (new_status, selected),
                )
                st.success(f"Status set to {new_status}.")
                st.rerun()

        st.markdown("#### Messages")
        messages = query(
            f"""SELECT author, message_text, created_at
                FROM {SCHEMA}.ticket_messages
                WHERE ticket_id = %s ORDER BY created_at""",
            (selected,),
        )
        for m in messages:
            with st.chat_message("user"):
                st.markdown(f"**{m['author']}** · {m['created_at']:%Y-%m-%d %H:%M}")
                st.write(m["message_text"])

        with st.form("add_message", clear_on_submit=True):
            author = st.text_input("Your name")
            body = st.text_area("Message")
            if st.form_submit_button("Add message"):
                if not author.strip() or not body.strip():
                    st.error("Name and message are both required.")
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
                st.error("Created by is required.")
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
                st.success(f"Created ticket #{new_id}.")
                st.rerun()