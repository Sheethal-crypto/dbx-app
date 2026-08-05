# Support Desk

A Streamlit app on Databricks Apps, backed by Lakebase (managed Postgres). Tickets and threaded messages, with create, comment, status change, filter, and delete.

Built for Day 1 of the DataExpert.io "Rise of the AI Data Engineer" bootcamp.

## Schema

Two tables in a dedicated `support` schema:

```sql
CREATE TABLE support.tickets (
    ticket_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','in_progress','resolved')),
    priority    TEXT NOT NULL DEFAULT 'medium'
                CHECK (priority IN ('low','medium','high')),
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE support.ticket_messages (
    message_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id    BIGINT NOT NULL
                 REFERENCES support.tickets(ticket_id) ON DELETE CASCADE,
    message_text TEXT NOT NULL,
    author       TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_messages_ticket
    ON support.ticket_messages(ticket_id, created_at);
```

Four decisions worth naming:

**CHECK constraints on status and priority.** An invalid status is rejected by the database, not just by the UI. The app is one client among potentially several, and the constraint holds regardless of which one writes.

**ON DELETE CASCADE on the foreign key.** Deleting a ticket removes its messages in the same transaction, with no application code. Without it, deleting a ticket leaves orphaned messages pointing at a row that no longer exists.

**Index on (ticket_id, created_at).** The dominant read is one ticket's thread in order. That is the OLTP access pattern this store exists to serve.

**Identity columns rather than serial.** Both are backed by a sequence, but a `serial` column's sequence is a separate object requiring its own `GRANT USAGE`. Miss it and reads succeed while every insert fails with a permission error on an object you never explicitly created. An identity column's sequence is owned by the column and accessed under the table's privileges, so `GRANT INSERT` on the table is enough. Since this app connects as a service principal rather than the schema owner, that difference matters.

## Authentication

Lakebase does not take a password. It issues short-lived OAuth tokens, and the app authenticates as its own service principal.

Adding a Database resource to the app injects `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and `PGSSLMODE`, and creates a Postgres role named after the service principal's client ID. `PGUSER` is that client ID. No credential is stored in this repo or in any config file.

The token expires, so it cannot be fetched once at startup. `lib/db.py` subclasses `psycopg.Connection` and mints a fresh token inside `connect()`, which the pool calls whenever it opens a new connection:

```python
class OAuthConnection(psycopg.Connection):
    @classmethod
    def connect(cls, conninfo="", **kwargs):
        cred = workspace.postgres.generate_database_credential(endpoint=endpoint)
        kwargs["password"] = cred.token
        return super().connect(conninfo, **kwargs)
```

`ENDPOINT_NAME` is a resource path (`projects/<project>/branches/<branch>/endpoints/<endpoint>`), not a hostname. It is not injected as an environment variable, so it is set in `app.yaml`.

The service principal still needs `USAGE` on the schema and DML on the tables. The Database resource grants `CONNECT` and `CREATE` on the database, not access to a schema someone else owns.

## Notes from building this

**Lakebase has two products with different APIs.** Autoscaling uses projects, branches, and endpoints, and calls `w.postgres.generate_database_credential(endpoint=...)`. Provisioned uses instances and calls `w.database.generate_database_credential(instance_names=[...])`. Most examples online show the Provisioned form. This project is Autoscaling.

**The pooled endpoint host rejected token auth.** Connecting to the `-pooler` hostname completed the TCP handshake and then failed SASL authentication. Behind a connection pool this surfaced only as a generic `PoolTimeout` after 30 seconds, with the real error swallowed. Bypassing the pool and connecting once directly is what exposed it. The direct host works.

**Debugging through a pool means debugging blind.** A pool reports that it could not get a connection, not why the connection failed. When a pooled connection fails for reasons you do not understand, the fastest move is a single direct `psycopg.connect()` with a short timeout, which raises the real exception.

**The workspace enforced Git-only app deployment.** With that setting on, the Lakebase Streamlit template is unavailable and `databricks sync` plus `databricks apps deploy --source-code-path` will not work. Deployment reads from a Git reference instead, which is why this repo exists.

## Layout

```
app.py                   Streamlit UI
lib/db.py                Connection pool and OAuth token refresh
lib/style.py             CSS and HTML render helpers
app.yaml                 Entry point and non-secret env vars
requirements.txt
.streamlit/config.toml   Theme
```

## Deploying

1. Create a Lakebase project and run the schema above
2. Grant the app's service principal `USAGE` on the `support` schema and `SELECT, INSERT, UPDATE, DELETE` on its tables
3. Create a custom Databricks App pointing at this repo, with a Database resource attached
4. Set `ENDPOINT_NAME` in `app.yaml` to your endpoint path
5. Deploy from the `main` branch
