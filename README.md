# Vehicle Safety Signal Finder

Enter your VIN, describe a car problem the way you'd describe it to a mechanic, and
find out whether other owners of the same vehicle have reported it, whether NHTSA has
an open recall, and what the engineering term for it is so you can name it at the
service desk.

Built on Databricks Free Edition for the DataExpert.io "Rise of the AI Data Engineer"
capstone.

---

## Requirement coverage

| Requirement | Where it lives |
|---|---|
| Data pipeline in Spark | Readable as markdown in `notebooks/01_ingest_bronze.md`, `02_silver_transform.md`, `03_gold_and_index.md`. 2,230,776 raw complaints down to 35,269 curated chunks |
| At least one third-party API | NHTSA vPIC, recalls, and vehicle models endpoints, all called live by agent tools in `app/vsf_tools.py` |
| Processing unstructured data | 2.2 million free-text complaint narratives, embedded with `databricks-gte-large-en` into a Delta Sync vector index. Chunking and index creation in `notebooks/03_gold_and_index.md` |
| Databricks App with a frontend | `app/app.py`. Three tabs: Garage, Ask, Insights |
| AI agent that takes real actions | Six tools in `app/vsf_tools.py`, three reads and three writes, native tool calling |

---

## The problem I set out to solve

NHTSA publishes every safety complaint it receives. The data is public and free and
close to unusable if you're an ordinary owner, because owners and engineers don't
describe failures the same way.

An owner types this:

> brake pedal feels soft and sinks toward the floor when I stop

The record that matches it is filed under `SERVICE BRAKES, HYDRAULIC`. Not one word
overlaps.

Embedding search closes that gap and opens a worse one. Semantic similarity has no
concept of which car you own. A Tundra brake complaint and a RAV4 brake complaint sit
almost on top of each other in vector space, so an unfiltered search hands you a
confident, well-written answer about somebody else's vehicle, and gives you no
indication that it has.

That silent failure is what this project measures and fixes.

---

## Results

Fifteen queries, written in owner symptom language, each labelled with the vehicle it
applies to and the component the right answer should carry. I wrote them without
reusing NHTSA's taxonomy wording, since reusing it would have tested nothing.

Both runs come out of the same function in `evals/run_eval.py`. The only difference is
the `--use-filters` flag.

| Metric | No filter | Filtered by vehicle |
|---|---|---|
| Right component **and** right vehicle at rank 1 | **0.00** | **0.33** |
| Right component and right vehicle in top 5 | **0.07** | **1.00** |
| Results from a different vehicle | 97.3% | 0% |
| Right component at rank 1, vehicle ignored | 0.80 | 0.33 |
| Right component in top 5, vehicle ignored | 0.93 | 1.00 |

Those last two rows need a word of explanation, because on their own they look like
the filter made things worse. It didn't.

The 0.80 is made entirely of correct component labels attached to the wrong car. A
Tundra brake complaint scores a hit on a RAV4 query, and the product can't use that
answer. Score component and vehicle together and not one of the fifteen unfiltered
queries put a usable result at rank 1. Only one had a usable result anywhere in its
top five.

So the real comparison is 0.00 to 0.33 at rank 1, and 0.07 to 1.00 on recall. Both
directions up.

Three of the ten remaining rank-1 misses put `UNKNOWN OR OTHER` first. That's NHTSA's
catch-all, it holds roughly 12 percent of the corpus, and since no query lists it as an
expected answer it can never score, even when the narrative underneath is a good match.
Take those out and rank 1 is 5 of 12. I'm reporting 0.33 as the headline; this is a
footnote to it rather than a substitute.

The seven genuine misses all sit at rank 2 to 4. Filtered recall hits 1.00 without ever
needing rank 5. So the correct answer is in the pool every single time, usually one or
two positions off the top, and what's left is a ranking problem rather than a retrieval
one. A reranker over five candidates is the obvious next move, and the ceiling it would
be reaching for is a measured 1.00 rather than a hopeful one.

Raw output: `evals/results_naive.json` and `evals/results_final.json`.

---

## The pipeline

### Bronze

One row per raw complaint. NHTSA's ODI flat file: 1.5 GB, tab delimited, no header,
51 fields, ISO-8859-1.

Three of the read options are load bearing and none of them are defaults. The separator
is a tab. Quote handling is switched off, because these narratives are full of
apostrophes and quote marks and Spark will otherwise fold records into each other and
lose rows without complaining. And the encoding isn't UTF-8.

Column order comes from `CMPL.txt`, the layout document NHTSA ships alongside the data.
I read it rather than assuming, which turned out to matter: fields 50 and 51 were added
in April 2026, so most references to this dataset still list 49.

I validate three things before moving on. Field count on the first record, parsed row
count against the raw line count, and a look at whether each column actually holds what
its name says. Row count alone won't catch a schema shifted by one, and a schema check
alone won't catch merged records.

The parse came back at 2,230,776 rows, matching the line count exactly.

### Silver

One row per complaint, narrowed to the vehicles in scope. Lexus and Toyota, 2013 and
later, narrative longer than 40 characters. 36,016 candidates, 35,269 survivors.

My reference vehicle is a Toyota RAV4, but the corpus deliberately spans eleven model
families. An index holding nothing but RAV4 complaints would make the baseline
meaningless, because an unfiltered search would have no way to return the wrong car.

VIN, dealer contact fields and `VEHICLE_OPERATOR` get dropped here and never reach the
application.

**Model names needed normalizing, and that turned into the most interesting thing in
this layer.** `MODELTXT` is inconsistent. The Lexus NX shows up as `NX`, `NX200T`,
`NX200`, `NX300`, `NX HYBRID`, `NX450H+`, `NX250`, `NX350H` and `NX350`. Filter on
`MODELTXT = 'NX'` and you get 319 rows for a vehicle line that actually has 430. `RX`
and `ES` have the same problem. There's also a literal `redundant SIENNA` value sitting
in the published data.

`model_family` folds all of that together, and I kept `model_raw` next to it so the
normalization is auditable instead of destructive.

The rule only strips a trailing engine or trim number when exactly two letters come
before it. That condition earned its place the hard way. My first version stripped any
trailing digit, which quietly turned `RAV4` into `RAV`. Nothing raised, the row count
didn't budge, and I only caught it because I checked the output against the model list
I was expecting instead of trusting the count.

`component_top` splits NHTSA's colon-delimited component hierarchy at the first level,
so `AIR BAGS:SENSOR:OCCUPANT CLASSIFICATION:FRONT PASSENGER` becomes `AIR BAGS`. The
distribution says something useful: `UNKNOWN OR OTHER` is the largest single category
at 4,185 records, about 12 percent. For one complaint in eight there is nothing in the
component field to filter on, which is precisely why the narrative has to carry the
search.

### Gold

One row per retrievable chunk. These narratives are short, so it's one chunk per
complaint with a length cap rather than splitting. Splitting would sever the link from
a search result back to a single complaint, and the citations depend on that link.
`chunk_id` is the real complaint id, already unique after deduplication.

---

## Why the index covers a subset

I pointed the vector index at all 35,269 Gold rows first. Databricks reported embedding
throughput of 1,256 ms per row against the shared pay-per-token endpoint, which put the
initial sync at 44,920 seconds. Twelve and a half hours. Free Edition also suspends
compute for the rest of the day once you exhaust quota, so an overnight embedding job
risks more than just the wall clock.

So the pipeline and the index are scoped differently, on purpose. The Spark pipeline
processes all 2,230,776 published complaints into 35,269 curated chunks. The vector
index covers a stratified 5,128-chunk subset: every Lexus record, and each Toyota model
family capped at 400. Keeping eleven families in there matters, because the cross-model
confusion is the thing the baseline is measuring.

One limitation I should name. The cap takes the first N rows ordered by `chunk_id`,
which correlates with time, so it samples a slice of history instead of a spread across
components. Sienna `SEATS` drops from 267 records to 8 while Sienna `STRUCTURE` holds
at 119. I checked the indexed distribution before writing the eval and swapped two
queries whose expected components had been thinned past the point of being measurable.
Given more time I'd stratify the sample by component.

---

## The agent

Six tools against `databricks-meta-llama-3-3-70b-instruct`, using native tool calling.

| Tool | Type | What it does |
|---|---|---|
| `search_complaints` | read | Filtered vector search, deduplicated on narrative text |
| `lookup_recalls` | read | Live NHTSA recall campaigns for the vehicle |
| `decode_vin` | read | vPIC lookup |
| `save_vehicle` | write | Persists the decoded vehicle |
| `add_vehicle` | write | Decodes a VIN and saves it, returning the existing record rather than a duplicate |
| `save_symptom_report` | write | Persists a symptom and the complaints matched to it |

Across 70 logged tool calls, `search_complaints` runs at 605.5ms p50 and `decode_vin` at
296ms.

`lookup_recalls` ran into the same problem this whole project is about, one layer down.
**NHTSA's two APIs don't use the same model names.** The complaints file records a 2017
Lexus NX under nine different strings. The recalls database only knows `NX200T`,
`NX300H` and `NX200`, and returns a 400 if you ask it about `NX`. So the tool doesn't
take a model name on faith. It asks NHTSA which models it knows for that make and year,
matches the family against that list, and queries each one. Same car, two vocabularies,
one federal agency.

Above the results, the Ask tab renders a synthesis card with four sections: whether the
pattern looks widely reported, recall status, **the engineering terms to give your
mechanic**, and the complaint ids it drew on. That third section is the thesis of the
project turned into something useful. You type "brake pedal feels soft" and it hands
back "service brakes, hydraulic".

The system prompt keeps it inside the records. No causes, no repairs, no parts, no
urgency.

### Agent-initiated writes

The write tools are not driven only by the UI. This prompt went through the agent loop
rather than the Garage form:

Prompt: `add VIN JTMD6RFV9RD126578 to my garage`

Response: "The vehicle with VIN JTMD6RFV9RD126578, a 2024 Toyota RAV4, is already in
your garage."

The event log recorded the model calling the tools itself:

```
2026-08-09T07:07:35  agent_answer                49970  true
2026-08-09T07:07:28  tool_call    add_vehicle    40139  true
2026-08-09T07:07:20  tool_call    save_vehicle    2397  true
2026-08-09T07:07:12  tool_call    decode_vin      1240  true
```

An `add_vehicle` row can only have come from the agent, since the Garage form calls
`decode_vin` and `save_vehicle` directly and never the composed tool.

The response is itself evidence of the idempotency guard. "Already in your garage" can
only come from the `exists` branch of `save_vehicle`, which returns the existing record
instead of inserting a second row. Full transcript in `notebooks/04_agent_tools.md`,
screenshots 21 and 22.

---

## Platform constraints, and what I did about them

I verified all of this before writing the code that depended on it, using a probe app
that tested each path in isolation. It's preserved at `app/probe.py`.

**Lakebase writes don't work on Free Edition.** I designed and created the relational
schema there: `users`, `vehicles`, `symptom_reports`, `matches`, `watches`, with foreign
keys and indexes. Writing to it from a notebook or app process is another story. Static
password auth is rejected outright, the server answers a personal access token with
"Provided authentication token is not a valid JWT encoding". The supported OAuth route
doesn't work either. Autoscaling projects don't appear in `list_database_instances()`,
the claims form fails with a Unity Catalog `DOES_NOT_EXIST` securable error, and
`RequestedClaimsPermissionSet` exposes exactly one value, `READ_ONLY`, which couldn't
have supported a writing agent anyway.

So application state writes to Delta tables that mirror that schema, and Lakebase holds
the relational model.

**Lakebase change data feed wouldn't start.** It needs a workspace admin to enable it
from the Previews page, and Free Edition doesn't expose that panel. It also refuses
destination catalogs on default storage, which is the only kind Free Edition gives you.
The instructor hit the same wall independently and later dropped it as a requirement. I
kept the piece anyway: the app writes an append-only event log to
`workspace.vsf.app_events`, a Delta table with change data feed enabled, and the
Insights tab reads back from it. The app reports on itself.

**Databricks Apps have no Spark session.** Writes go through the SQL statement
execution API against a warehouse, with bound parameters rather than interpolated SQL,
because complaint narratives and user-typed symptoms are full of apostrophes.

**Apps run as a service principal, not as you.** It needs explicit grants: `USE
CATALOG`, `USE SCHEMA`, `SELECT` and `MODIFY` on the schema, plus `CAN USE` on the
vector search endpoint. `VectorSearchClient` can't infer credentials there either, and
`WorkspaceClient().config.host` returns a bare hostname which that client then
concatenates into a URL without adding a scheme.

---

## Change data feed

Change data feed is enabled on the event log at the table level:

```
SHOW TBLPROPERTIES workspace.vsf.app_events;

delta.enableChangeDataFeed      true
delta.feature.changeDataFeed    supported
delta.minReaderVersion          3
delta.minWriterVersion          7
```

`workspace.vsf.app_events_analytics` is materialized from that feed rather than from the
table itself, reading `table_changes('workspace.vsf.app_events', 0)` filtered to
`_change_type = 'insert'` and grouped by tool.

```sql
CREATE OR REPLACE TABLE workspace.vsf.app_events_analytics AS
SELECT tool_name,
       count(*) AS calls,
       percentile(latency_ms, 0.5) AS p50_ms,
       percentile(latency_ms, 0.95) AS p95_ms,
       sum(CASE WHEN NOT ok THEN 1 ELSE 0 END) AS failures,
       max(_commit_timestamp) AS last_change
FROM table_changes('workspace.vsf.app_events', 0)
WHERE _change_type = 'insert' AND event_type = 'tool_call'
GROUP BY tool_name
ORDER BY calls DESC;
```

Current contents, with `last_change` omitted:

| Tool | Calls | p50 ms | p95 ms | Failures |
|---|---|---|---|---|
| `search_complaints` | 36 | 605.5 | 4500.25 | 0 |
| `add_vehicle` | 9 | 2276 | 7243.2 | 1 |
| `lookup_recalls` | 8 | 862.5 | 1345.95 | 1 |
| `decode_vin` | 7 | 296 | 439.3 | 1 |
| `save_vehicle` | 6 | 1448 | 3156.25 | 0 |
| `save_symptom_report` | 4 | 2738.5 | 10302.75 | 1 |

---

## Repository

```
notebooks/     01 ingest, 02 silver, 03 gold and index, 04 agent tools
               each as .ipynb with an exported .md
               05 lakebase setup, 06 cdf analytics, markdown only
app/           app.py, vsf_tools.py, probe.py
evals/         queries.json, run_eval.py, results_naive.json, results_final.json
notes/         eval_writeup.md, results.md
spec/          pipeline_spec.md
screenshots/
```

Every notebook is provided as markdown in `notebooks/*.md`, so the pipeline code
can be read in full without a notebook environment.

`app/vsf_tools.py` is the implementation the deployed app imports. An older copy
predating the `decode_vin` / `save_vehicle` split still sits in my workspace, and
`notebooks/04_agent_tools.ipynb` keeps the inline `add_vehicle` prototype it had before
the shared module existed.

`spec/pipeline_spec.md` was written before any code and revised as I measured things.

---

## Running it

The notebooks run in order against Databricks serverless. Notebook 01 downloads the ODI
flat file itself, so nothing needs supplying.

The evals need a Databricks host and token in the environment:

```
python evals/run_eval.py --out evals/results_naive.json
python evals/run_eval.py --out evals/results_final.json --use-filters
```

The app deploys from a Git repository, which is the only route Free Edition supports.

---

## Scope

I fixed scope up front so that every cut would be a decision rather than an accident.

Out: any make outside Lexus and Toyota, model years before 2013, technical service
bulletins, and an eval set larger than fifteen queries. The make and year filters are
parameters, so widening coverage is a config change, not new code.

Two features got built and then removed. Voice input via browser speech recognition
worked locally but broke the app deployment, and a hero image on the empty state wasn't
worth another deploy cycle to keep. Neither was a requirement.

---

## What this is and isn't

Everything the app shows comes from NHTSA's published records, cited by complaint id.
It isn't a diagnosis and it isn't repair advice.
