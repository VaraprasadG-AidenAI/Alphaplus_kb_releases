# The knowledge base

Turns an AS/400 source tree into a queryable set of facts — what calls what,
what opens which file, what breaks if you change something — and serves it to
a coding agent over MCP.

Reads a source tree and nothing else - no generator, no catalog.

## Where data lives

One canonical store; everything else is a derived, single-purpose index
rebuilt from it. `kb.graph.selftest` enforces the boundary that used to drift.

| store | holds | selected by |
|---|---|---|
| **PostgreSQL** | every parsed fact (29 tables), the written pages, embedding bookkeeping | `ALPHAPLUS_PG_DSN` - configuring it *is* choosing postgres |
| **SQLite** | the same schema, in one file | no DSN configured, or `ALPHAPLUS_STORE=sqlite`. The zero-install mode |
| **Neo4j** | keys and relationships - traversal questions only, never document text | `ALPHAPLUS_GRAPH_BACKEND` (default `neo4j`; `sqlite` answers the same tools relationally, `compare` runs both and reports any disagreement) |
| **Qdrant** | one vector per written page + the serving payload (scope, key, library, preview) | `ALPHAPLUS_QDRANT_URL` for a server; unset runs embedded - a directory, no service |

Postgres and Neo4j are optional in the same sense: without them the KB runs
entirely from the SQLite file with the graph tools answered relationally.
Vectors are required *for semantic routing* and for nothing else - without them
`route_ticket` degrades to bm25 and says so.

---

## 1. Get the source onto a filesystem

The KB does not talk to IBM i. Extract first, one file per member, **member
type as the extension**:

```
APPALPH/QCBLLESRC/ORD100.CBLLE     APPALPH/QDDSSRC/CUSMAS.PF
APPALPH/QCLSRC/ORD100C.CLLE        APPALPH/QDDSSRC/ORD100D.DSPF
ALPH/QCPYSRC/DBFLAGS.CPY           SERVPLUS/QSQLSRC/TAB_ORD.SQL
```

Usual routes are `CPYTOSTMF` per member, or whatever the shop's existing
extract job produces.

**The extension decides the member type. The path decides the library.** The
library is the directory holding the source physical file, so
`APPALPH/QCBLLESRC/ORD100.CBLLE` is in `APPALPH`. Nest it one deeper
(`EXTRACT/APPALPH/QCBLLESRC/...`) and it still resolves to `APPALPH`. A flat
tree with no library level is fine too — every member reports `''` and
`kb.index.status` says so plainly rather than inventing a name.

Beyond that the layout is free.

Then point the KB at it:

```bash
set ALPHAPLUS_SOURCE=D:\extract\APPSRC      # Windows
export ALPHAPLUS_SOURCE=/srv/extract/APPSRC # Linux
python -m kb.config                          # confirm what it sees
```

## 2. Check the conventions before building

Four things in [`config.py`](config.py) are guesses until a real extraction
arrives. Read them once:

| Constant | Fix it when |
|---|---|
| `EXT_TO_TYPE` | your extract uses different extensions (`.CBL` vs `.CBLLE`, `.TXT` for copybooks) |
| `TYPE_TO_LANG` | you have a member type not listed |
| `LIBRARY_PRIORITY` | the extract contains DEV/TEST/PROD copies — list them in priority order, first wins, rest reported as duplicates |
| `IGNORE_DIRS` | there are directories to skip |

Nothing else needs editing to move to a new codebase.

## 3. Build - one command

```bash
python -m kb sync                # index -> lexicon -> graph -> map, from zero or stale
```

`sync` does the least work that is correct: no index yet means a full parse
(creating the PostgreSQL schema itself if a DSN is set), an empty Neo4j means a
full graph load (constraints included), an existing index means an incremental
refresh of only what changed. `--watch` keeps doing it on a timer.

The pieces individually, when you want just one:

```bash
python -m kb.index.build         # full parse into the configured store
python -m kb.index.status        # what got parsed, what didn't
python -m kb.index.consistency   # contradictions that need no ground truth
python -m kb.graph.load          # facts -> Neo4j (applies its own constraints)
python -m kb summarise           # the written pages (needs an LLM; see below)
python -m kb semantic            # embed the pages into the vector store
python -m kb doctor              # is this install healthy, end to end
python -m kb.e2e                 # every stage, one verdict; --all for all combos
```

Then start the server - `python -m kb serve` (stdio, for an agent) or
`python -m kb serve-http` (loopback port, token-protected).

---

## Commands

### Index

| Command | Does | Options |
|---|---|---|
| `kb.index.build` | full parse into `facts.db` | `--limit N` first N members · `--workers N` (8) · `--quiet` |
| `kb.index.update` | reparse only changed members | `--dry-run` · `--verify` compare against a full rebuild · `--quiet` |
| `kb.index.status` | coverage: parsed, partial, failed, unresolved | — |
| `kb.index.consistency` | self-checks needing no gold data | `--list` · `--check KEY` · `--show N` |
| `kb.index.check` | parser accuracy vs gold (replica only) | `--show N` |

### Lexicon

| Command | Does |
|---|---|
| `kb.lexicon.terms` | build the auto-seeded half of the term glossary from the indexed codebase. The hand-written half lives in `terms.py`. |

### Summaries (optional, off by default)

Everything else works with this switched off, and `none` is a real setting —
pages get written from extracted facts and nothing leaves the machine.

**Start here.** Local model, nothing leaves the network:

```powershell
$env:ALPHAPLUS_SUMMARIES  = "1"
$env:ALPHAPLUS_LLM        = "local"
$env:ALPHAPLUS_LLM_MODEL  = "qwen3-coder:30b"
$env:ALPHAPLUS_LLM_SOURCE = "1"

python -m kb.summarise.provider          # does the model answer at all?
python -m kb summarise --estimate        # what will it cost?
python -m kb summarise --level menu --limit 3   # read three before trusting 92
python -m kb summarise                   # the whole pyramid
```

`bash`/`cmd` users: `export ALPHAPLUS_SUMMARIES=1` / `set ALPHAPLUS_SUMMARIES=1`.

| Command | Does |
|---|---|
| `kb summarise` | write the pyramid; resumes, skipping pages still fresh |
| `kb summarise --estimate` | units, prompt size and hours, from measured throughput |
| `kb summarise --status` | how many pages exist per level, and what is missing |
| `kb summarise --level menu\|family\|library` | one level only |
| `kb summarise --limit N` | first N of each level — use it before a full run |
| `kb summarise --dry-run` | build every prompt and print its size, call nothing |
| `kb summarise --refresh` | rewrite pages that are still fresh |

It commits per page, so Ctrl+C costs one page and re-running picks up where it
stopped. On the replica: 112 pages, about two hours on a CPU-only machine.

Once pages exist, embed them for semantic routing:

```bash
python -m kb semantic            # embed pages -> vector store; resumable
python -m kb semantic --check    # model, dimension and count all agree?
```

Embedding needs the model at `ALPHAPLUS_EMBED_URL` (default: Ollama,
`snowflake-arctic-embed2`). The vector store holds one point per page and the
payload to serve a hit; the bookkeeping that decides staleness lives in the
`embedding` table. Semantic routing switches itself on when vectors and the
model are present (`ALPHAPLUS_SEMANTIC=0` forces it off).

#### Four levels, and the bottom one has no model in it

| Level | Units | Written by |
|---|---|---|
| flow | 1,105 menu options | **facts, rendered on demand** |
| menu | 92 | model |
| family | 17 menu areas | roll-up of menus |
| library | 4 source libraries | roll-up of families |

A menu option's facts already read as a sentence — option, label, driver,
program, files changed — so prose for 1,105 of them would take eleven hours to
say something less exact than the facts it came from. The model is spent where
synthesis across many options is the point.

The family level is not decoration. `ALPH` holds 61 menus; rolling those
straight into one library page is ~9,000 tokens, six minutes of prefill on a
CPU and past the model's context. Eleven families collapse it to ~600.

A library with **no menus** is handled, not special-cased: `SERVPLUS` owns none
and is entered by 100 menu options in `ALPH`, so its page is built from the
inbound flows and its own inventory and says plainly that nothing in it is
reached directly by a user. On a real 21-library tree that will be normal.

#### Two things about a local model

**Set the context you have, and know it is not settable.** Ollama's
OpenAI-compatible endpoint ignores `num_ctx`, so a prompt longer than the loaded
context is truncated with **no error** and the model summarises the fragment.
Since it cannot be set, it is checked: `ALPHAPLUS_LLM_CONTEXT` (default 4096)
states what the model was loaded with, and a prompt that will not fit is
refused rather than quietly mangled.

**Pages are reproducible.** `temperature` and `seed` are pinned, at the top
level of the request where that endpoint actually reads them. Without it the
same menu gave "back-office maintenance and audit functions" on one run and
"back-end maintenance, data extraction, file building" on the next, from
identical facts — and a page nobody can reproduce cannot be reviewed.

#### The member tier

The older per-member summariser is still there and unchanged:

| Command | Options |
|---|---|
| `kb.summarise.build` | `--limit N` · `--deep N` full source for the N most-depended-on members · `--stale` only what changed · `--estimate` · `--status` · `--dry-run` |

`--deep N` takes the top N of the ranking the build already uses: fan-in first,
then size. It used to be churn, and "most-changed" survived in three places
after the `change` table was removed. The deep tier does nothing unless
`ALPHAPLUS_LLM_SOURCE=1` — without it there is no source to send.

### Server

| Command | Does | Options |
|---|---|---|
| `kb.server.server` | MCP server over stdio | `--selftest` run every tool once and exit |

### Graph map

| Command | Does | Options |
|---|---|---|
| `kb.viz.build` | one self-contained HTML map | `-o PATH` · `--force-layout` adds a force layout (needs numpy) · `--neo4j` also write the import set · `--db PATH` |
| `kb.viz.graph` | print node/edge counts and gaps | — |

### Benchmark (replica only)

| Command | Does | Options |
|---|---|---|
| `kb.eval.run` | run the ticket benchmark | `--arm baseline\|kb` · `--limit N` · `--ticket ID` · `--timeout S` · `--resume` · `--report` |
| `kb.eval.export_gold` | export ground truth from the generator | — |

---

## The 23 MCP tools

Organised by direction, because that is how a question arrives.

| | Tool | Answers |
|---|---|---|
| **outward** | `what_it_touches` | what does this member reach — files, copybooks, calls, overrides, screens, messages, data areas, SQL |
| **inward** | `what_uses` | what reaches this object, and the blast radius of changing it — it works out for itself whether you gave it a file, copybook, format, message id, data area or field |
| **inside** | `program_structure`, `what_paragraph_does` | how is this member built |
| **by name** | `trace_ticket`, `search_source`, `find_paragraph` | I have a word, not a name |
| **by meaning** | `route_ticket`, `find_similar` | which *area* is this about - cosine over the written pages, fused with bm25 |
| **graph-shaped** | `blast_radius`, `path_between`, `menu_path_to` | one-round-trip traversals; answered by Neo4j |

The others are the well-worn paths through the same facts. They stay
because an agent that already knows what it wants should not have to filter a
general answer, and because a cheap targeted call beats an expensive general one
when you already know which you need — `find_object` settles "does ORD100 exist"
in 293 characters where `what_it_touches` spends 1,100.

`impact_of_change` was removed. `what_uses` answered every branch of it, across
more layers, with large sets folded by group instead of truncated at forty
names — and it did not mistake a display file for a program, which
`impact_of_change` did.

### One question, one predicate

`describe_file` used to say `CUSMAS` was "written by 3" while
`who_touches_file` said 5, because one read only the `OPEN` mode. Two tools
disagreeing on the same question is worse than either being wrong alone: an
agent cannot tell which to believe. Every tool that asks "can this program
change this file" now shares a single SQL predicate, `_CAN_CHANGE`.

| Tool | Answers | Args |
|---|---|---|
| `what_it_touches` | everything one member reaches, by layer | `name`, `kind`, `limit` |
| `what_uses` | everything that reaches one object, any kind | `name`, `kind`, `limit` |
| `find_object` | what is this member, where, what type | `name` |
| `who_calls` | what reaches this program | `name`, `depth` |
| `what_it_calls` | what this program reaches | `name`, `depth` |
| `who_touches_file` | which programs read/write a file | `file`, `mode` |
| `where_is_field_used` | which programs use a field, read or write | `field`, `direction` |
| `describe_file` | fields, keys, logicals over it | `file` |
| `explain_screen` | formats and fields of a display file | `device_file` |
| `resolve_overrides` | which physical file a program really opens | `program` |
| `read_member` | source, paged | `name`, `start`, `count` |
| `search_source` | regex over source, filtered by member type | `pattern`, `member_type` |
| `read_summary` | the written page for a member, menu, menu area or library | `name` |
| `coverage_report` | where the KB is blind | — |
| `trace_ticket` | free text in, candidate members out | `text`, `limit` |
| `program_structure` | the paragraph graph of one COBOL program | `name`, `member_type`, `max_rows` |
| `what_paragraph_does` | one paragraph: performed by, performs, reads, writes, calls | `program`, `paragraph`, `member_type` |
| `find_paragraph` | paragraphs anywhere that touch a file, call a program or set a field | `name`, `touches`, `calls`, `writes`, `limit` |
| `blast_radius` | everything a change could reach, in one round trip | `name`, `depth` |
| `path_between` | every shortest route between two members, each hop labelled | `frm`, `to`, `edges`, `max_hops`, `max_paths` |
| `menu_path_to` | how a user reaches this member from a menu | `name`, `depth` |
| `route_ticket` | free text in, the menus/areas/libraries it is about | `text`, `limit` |
| `find_similar` | written pages that read like this one's | `name`, `limit` |

`--selftest` exercises all of them without an agent.

### Built for a small context window

The codebase is 40GB and the answers have to fit in a window alongside the
work. Three rules, and they are the reason `what_uses` is 1.5k characters on a
file 1,847 programs open rather than 6k:

* **`kind='all'` is a routing table, not an answer.** It gives the count per
  layer and expands only what is short enough to read. Ask for one layer to get
  it in full.
* **Large sets fold to one row per group.** 1,109 members that COPY `CUSMAS`
  becomes 37 groups with counts. The shape of the set is what decides where to
  look; the names are available by raising `limit`.
* **Never fold away the writers.** A file two thousand programs read is usually
  changed by five, and those five are the answer to *what put this value here*.
  They are always listed.

---

## Inside one program

A COBOL program has no functions. It has paragraphs, it performs them in a
shape, and that shape is the only high level structure the language offers.
`program_structure` recovers it:

```
AGD109  (CBLLE, 813 lines)
  28 paragraphs, 30 control edges, 2 in-line loops

    line  structure
  ------  ---------
     298  0000-MAIN-LINE
     301    1000-INITIALISE   NOT DECLARED IN THIS MEMBER
     363    2000-PROCESS-SCREEN   1 in-line loop(s)
     375      2100-CLEAR-SUBFILE   write AGD109D/SFLCTL
     389      2200-LOAD-SUBFILE   read GLAX05L3; 1 in-line loop(s)
     410      [inside a loop] 2300-DISPLAY-SUBFILE   write AGD109D/HEADER,SFLMSG
     713        8100-SEND-MESSAGE   CALL ERRSRV
```

Then `what_paragraph_does AGD109 2200-LOAD-SUBFILE` for one node, or
`find_paragraph touches=CUSMAS` to go the other way - from a file to the twenty
lines that write it, not the sixty programs that open it.

### PERFORM is two statements sharing a keyword

Getting this wrong is the reason a naive paragraph graph is worse than none.

| Written | What it is | Recorded as |
|---|---|---|
| `PERFORM 2000-PROCESS` | a call, once | edge, `form=simple` |
| `PERFORM 2000-PROCESS THRU 2900-END` | a call over a range | edge, `form=thru` |
| `PERFORM 2000-PROCESS UNTIL EOF` | a call, repeatedly | edge, `is_loop=1` |
| `PERFORM 2000-PROCESS 5 TIMES` | a call, repeatedly | edge, `is_loop=1` |
| `PERFORM UNTIL EOF ... END-PERFORM` | a loop, **no target** | `inline=1`, no edge |
| `PERFORM 5 TIMES ... END-PERFORM` | a loop, **no target** | `inline=1`, no edge |
| `PERFORM WS-COUNT TIMES ...` | a loop, **no target** | `inline=1`, no edge |
| `GO TO 9999-EXIT` | a jump, does not come back | edge, `form=goto` |

`PERFORM WS-COUNT TIMES` is the trap: the count is spelt exactly like a
paragraph name and only the `TIMES` that follows tells them apart. Treat it as
a target and the graph grows a paragraph the program does not have.

A third column, `in_loop`, marks a plain `PERFORM` written *inside* somebody
else's in-line loop - it still runs once per iteration. The depth is counted
per COBOL sentence, because a separator period terminates an unterminated
in-line `PERFORM` just as legally as `END-PERFORM` does.

### What it does not cover

CL and RPG. CL members have labels but almost always exactly one, so the graph
would be a single node; RPG procedures exist only in free-format members, of
which this codebase has 31. Both are recorded in `paragraph` but neither gets a
`perform_edge`. The member-level graph map (`kb.viz.build`) is also unchanged -
it stays at member granularity.

---

## Libraries, and the layer above the graph

The graph is only reachable from a name. A ticket that names no member — and
that is most of them — had no way in. These are the facts that give it one.

### Libraries

`object.library` and `member_hash.library` come from the path: the library is
the directory holding the source physical file, so
`APPOBJ/QCBLLESRC/ORD100.CBLLE` is in `APPOBJ`. A flat tree with no library
level reports `''` rather than having a name invented for it.

`LIBRARY_PRIORITY` decides which copy of a duplicated name is indexed. The
loser is recorded as `unresolved` kind `duplicate_member` — never dropped —
because every relationship table is still keyed on the bare member name, so a
duplicated name has the facts of two members merged. That count is the
measurement that decides whether library-qualifying the edge tables is worth
paying for. `call_edge.callee_library` keeps the qualifier a CALL was written
with, which is the evidence for telling them apart later.

### `tier` and `depth`

Two different questions, kept apart.

| | |
|---|---|
| `tier` | what kind of thing this is: `entry`, `driver`, `program`, `service`, `contract`, `data`. From member type plus in-degree — never from a naming convention. A program the rest of the application depends on becomes `service` at the same 99th-percentile cut the graph map uses for hub damping. |
| `depth` | CALL hops from the nearest entry point. Files and copybooks inherit the shallowest depth of a member that reaches them. |

Both are recomputed over the whole index on every write, including an
incremental update, because a member's depth can change when a *different*
member starts calling it.

**`depth IS NULL` is not dead code.** On this codebase 2,953 of 3,654 CL
programs are unreached, and that is the expected answer: nothing in the source
says what runs a program overnight. The job scheduler is a permanent blind
spot, so an unreached batch program may run every night.

### Menu options

A menu is two members sharing a name and nothing else — the `MNUDDS` screen and
the `MNUCMD` command list. `menu_option` is the join, and the join key is the
option number, which the source writes **twice, independently**:

| Half | Where the number is |
|---|---|
| `MNUCMD` | the nth non-blank non-comment line is option n |
| `MNUDDS` | the constant text starts with it: `' 3. Audit recalculation'` |

Both are executable source and neither is a comment, so the two halves
cross-check each other rather than one being trusted. Agreement is `exact`; one
half alone is `inferred`; a disagreement is recorded and reported, never
reconciled by guessing.

Reading the ordinal out of the text also separates options from furniture for
free — 644 of 1,749 menu constants are titles, rules, `Selection or command`
and `F3=Exit`, and none of them carry a leading number.

Option labels are the one piece of business language a user actually quotes, so
they now reach the FTS index. `search MATCH 'audit recalculation'` ranks
`APMAIN` first; before this the FTS body for a menu was its own name and
nothing else. Scoped to menus deliberately — adding every screen and report
constant would bury the 91 entry points under 12,175 field prompts.

## Two modes on `file_usage`, and why

`mode` is what the `OPEN` declared. `verbs` is what the statements actually do.
They disagree on 378 rows here — always a `WRITE`, `REWRITE` or `DELETE` on a
file opened `INPUT`, which valid COBOL cannot do because the compiler rejects
it.

Neither column is folded into the other. Folding the verbs into the mode
asserts something impossible and cost 19 points of precision against the gold
data; ignoring them hid 294 programs across 234 files from *what writes this
file*. So both are kept, `kb.index.consistency --check write_on_input_open`
reports every disagreement, and any query asking what can change a file reads
both columns and marks the contradictory rows `(!)`.

That is the general rule in this schema: where two facts disagree, record both
and report the disagreement. Picking one silently is how a knowledge base
starts lying.

---

## The graph map

```bash
python -m kb.viz.build --force-layout        # kb/data/graph.html, ~2.8 MB
```

Open it off the filesystem — no server, no CDN. 13k members, 132k edges, one
toggleable layer per edge kind (CALL, COPY, file I/O, OVRDBF, screen, LF-over-PF,
message, data area). Click a member for its facts and a tiered blast radius:
Tier 1 must recompile (exact), Tier 2 must review (hub-damped), Tier 3 notify
only. Deep links: `graph.html#ORD100,blast`.

Details in [`docs/kb/06-GRAPH-MAP.md`](../docs/kb/06-GRAPH-MAP.md).

### Neo4j - the live graph

The CSV-into-Desktop route is gone; `kb/graph/` loads Neo4j directly and keeps
it in step:

```bash
python -m kb.graph.load          # bulk load; creates constraints, verifies
python -m kb.graph.update        # refresh only members whose hash moved
python -m kb.graph.selftest      # 8 checks, including "no document text here"
python -m kb.graph.bench         # sqlite | postgres | neo4j, timed, compared
```

Point it at a server with `ALPHAPLUS_NEO4J_URI/_USER/_PASSWORD`, or let
`[neo4j.autostart]` in alphaplus.toml start a local one
(`ALPHAPLUS_NEO4J_HOME` names the unpacked install). The graph is a derived
index: rebuilding it from the relational store is always safe, and it holds no
text - full-text search is answered by the relational store alone.

---

## Rules the code keeps

**Nothing under `kb/` imports from `tools/`.** In a real engagement you get a
source tree and nothing else. The one exception is `eval/export_gold.py`, which
exists only to score the replica.

```bash
grep -rnE "^\s*(from|import)\s+(gen|tools)\b" kb/ --include=*.py
```

**Facts come from executable code, never comments.** Header blocks are parsed
only as a cross-check; a mismatch is recorded in `header_disagreement` as a
finding, not treated as fact.

**Say what you don't know.** Every fact carries `confidence`
(`exact` / `inferred` / `unresolved`) and `evidence` (`member:line`). A
`CALL WS-PGM`, an `OVRDBF` with a variable library, an unresolvable copybook —
all recorded in `unresolved`, never dropped. The job scheduler is not in the
source and never will be; a member with no inbound edge is not proof it is dead.

---

## Layout

| | |
|---|---|
| `config.py` | the only file to edit when moving to a new codebase |
| `parse/` | one parser per language — cobol, cl, dds, cpy, misc |
| `parse/cobol.py` | also the PERFORM/GO TO classifier and the paragraph attribution pass |
| `index/` | build, incremental update, status, consistency checks |
| `index/schema.sql` | the fact tables, with what each column means |
| `server/` | MCP server and the 23 declared tools; `routing.py` bm25+cosine fusion; `semantic.py` the vector search |
| `store/` | where data lives: the relational seam (sqlite/postgres), `vectors.py` (embedded/server Qdrant), DDL, the staging-schema swap |
| `graph/` | Neo4j: model (one spec per table), loader, incremental refresh, selftest, bench |
| `viz/` | the graph map (React viewer + classic) |
| `lexicon/` | domain term glossary |
| `summarise/` | optional written summaries — `pyramid.py` menus/areas/libraries, `sheets.py` what each level is written from, `store.py` where they live |
| `eval/` | ticket benchmark, replica only |
| `sync.py` | the pipeline: index -> lexicon -> graph -> map, from zero or stale |
| `e2e.py` | every stage in order, one verdict, per store/graph combination |
| `<source>/_alphaplus-kb/` | `facts.db` (sqlite mode), summaries, `graph.html`, embedded vectors — all generated, all gitignored |
