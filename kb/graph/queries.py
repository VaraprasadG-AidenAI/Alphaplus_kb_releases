"""
Cypher for the tools that map cleanly onto the graph.

Each function returns the same row shape the SQLite version returns, so
`kb/server/tools.py` can switch backends without its formatting changing.  That
is the whole reason this module exists as a layer rather than Cypher inline in
the tools: with the shapes identical, `backend = "compare"` can run both and
diff them.

## What is here, and what is deliberately not

Here: the traversal questions.  `who_calls`, `what_it_calls`, blast radius,
paths, logical-file bridging, field usage, override resolution.  These are the
questions a graph answers better, and several are ugly in SQL.

Not here: anything full-text (FTS5 `bm25()` has no Neo4j equivalent) and the
big aggregate reports.  Those keep reading SQLite.  `model.STAYS_IN_SQLITE`
lists them with reasons.

## Parity with SQLite holds at depth 1, and deliberately not beyond

At depth 1 these functions and their SQLite counterparts agree row for row -
verified over 60 comparisons with `backend = "compare"`.

At depth 2 and beyond they diverge on purpose, because a caller can reach the
target through several level-1 members:

  * `sites` - SQLite counts the call sites on the one path its breadth-first
    walk happened to take.  Cypher collects DISTINCT line-sets across every
    path, so it reports more: 141 of 1,647 rows for ERRSRV at depth 2.  The
    Cypher number is the true count of places the caller makes a call that
    leads to the target.
  * `line` - with several paths there is no canonical first line.  Each store
    reports one from the path it took, and they differ on 35 of those rows.

Neither is a defect and the difference is not worth removing: matching SQLite
exactly at depth 2 would mean throwing away paths Cypher has already found.
`compare` will report these as disagreements, and that is correct - they are.

## Every traversal is bounded

`*1..{hops}` with hops from config, never `*`.  The call graph is cyclic - CL
drivers call programs that call CL drivers - and an unbounded variable-length
match on a cyclic graph does not return.  A bounded one that misses a 9-hop
path is a wrong answer; an unbounded one that hangs is a broken tool, and the
bounded wrong answer at least says how far it looked.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from kb.graph import driver
from kb.graph import settings as gs


def _hops(override: Optional[int] = None) -> int:
    cfg = gs.load()
    want = override if override is not None else cfg.query.max_hops
    return max(1, min(want, cfg.query.max_hops))


# ---------------------------------------------------------------------------
# who_calls / what_it_calls
# ---------------------------------------------------------------------------

def who_calls(name: str, depth: int = 1) -> List[Dict[str, Any]]:
    """Callers, level by level.

    `min(length(p))` because the same caller can reach the target by more than
    one path and the shortest is the one worth reporting - a program two hops
    away should not be listed at level 5 as well.
    """
    h = _hops(depth)
    return driver.read(f"""
        MATCH p = (caller:Member)-[:CALLS*1..{h}]->(target:Member {{name: $name}})
        WHERE caller.stub IS NULL
        // Group by NAME, not by node.  The same member name can exist as more
        // than one node - two libraries, or a PF and its LF - and each carries
        // its own CALLS edge.  call_edge in SQLite stores only the name, so it
        // cannot tell them apart either; one row per name is the honest answer
        // and it keeps the two stores comparable.
        WITH caller.name AS caller, min(length(p)) AS level,
             // min(), not head(collect(...)): collect has no defined order, so
             // when a name resolves to several nodes this picked an arbitrary
             // relationship where the relational query takes MIN. See
             // what_it_calls for the case that exposed it.
             min(relationships(p)[0].kind) AS kind,
             min(relationships(p)[0].confidence) AS confidence,
             min(toInteger(
                 split(coalesce(relationships(p)[0].lines,'0'), ',')[0])) AS line,
             // Site count over DISTINCT line-sets, not sum() over paths.
             // 107 names exist as two members - the AS/400 menu convention,
             // where a menu is an MNUDDS screen plus an MNUCMD command list,
             // same name, two types.  Both are real members so both are nodes,
             // but a call_edge row naming the menu becomes a relationship on
             // each, and sum() would then double-count the one call site.
             // The duplicated relationships carry identical `lines`, so
             // collecting DISTINCT line-sets and counting the entries gives the
             // true number and matches SQLite exactly.
             collect(DISTINCT coalesce(relationships(p)[0].lines, '')) AS ls
        WITH caller, level, kind, confidence, line,
             reduce(t = 0, x IN ls | t + size(split(x, ','))) AS sites
        RETURN caller, level, kind, confidence, line, sites
        ORDER BY level, caller
    """, {"name": name.strip().upper()})


def what_it_calls(name: str, depth: int = 1) -> List[Dict[str, Any]]:
    h = _hops(depth)
    return driver.read(f"""
        MATCH p = (src:Member {{name: $name}})-[:CALLS*1..{h}]->(callee:Member)
        WHERE callee.stub IS NULL
        // See who_calls: group by name, not by node.
        // collect() has no defined order, so head(collect(...)) was picking an
        // arbitrary relationship when a callee NAME resolves to more than one
        // node. The relational query takes MIN(line) across all of them, and
        // reported 618 where this reported 6230 for ORD100 -> SYS100 - same
        // 37 sites, different line cited. min()/max() here rather than head(),
        // so the two stores answer the same question and this one is
        // reproducible run to run.
        WITH callee.name AS callee, min(length(p)) AS level,
             min(relationships(p)[0].kind) AS kind,
             min(relationships(p)[0].confidence) AS confidence,
             min(toInteger(
                 split(coalesce(relationships(p)[0].lines,'0'), ',')[0])) AS line,
             collect(DISTINCT coalesce(relationships(p)[0].lines, '')) AS ls
        WITH callee, level, kind, confidence, line,
             reduce(t = 0, x IN ls | t + size(split(x, ','))) AS sites
        RETURN callee, level, kind, confidence, line, sites
        ORDER BY level, callee
    """, {"name": name.strip().upper()})


#: The relationship types that join one member to another.
#:
#: There are 20 edge types in the model and these are the 9 whose start and
#: end are both a Member, so these are the ones a path from member to member
#: can be made of.  The rest end at a Field, a Paragraph, a Format, a Message,
#: a DataArea, an Indicator or a Library, and a route that goes through one of
#: those does not arrive at a member.
#:
#: Not a description of importance.  A COPIES hop and a CALLS hop are both
#: real dependencies and they mean entirely different things - one is a
#: rebuild, the other is control flow - so every hop is returned labelled
#: with its type and the caller decides what the mixture is worth.
MEMBER_EDGES: Dict[str, str] = {
    "CALLS": "calls it",
    "OPENS": "opens it as a file",
    "COPIES": "COPYs it",
    "BASED_ON": "is a logical over it",
    "OVERRIDES_TO": "overrides a file to it",
    "RESOLVES_TO": "really opens it once the override is applied",
    "MENU_OPTION": "is a menu option that runs it",
    "EXEC_SQL": "reaches it through embedded SQL",
    "IO_AT": "does I/O on it",
}

#: What `edges="calls"` means, kept separate from the full set because it is
#: the common question - "how does control get from here to there" - and
#: mixing data and build edges into that answer buries it.
CALL_EDGES = ("CALLS",)


def path_between(frm: str, to: str, edges: str = "calls",
                 max_hops: Optional[int] = None,
                 max_paths: int = 8) -> List[Dict[str, Any]]:
    """Every shortest route from one member to another, each hop labelled.

    This is the query that is genuinely awkward in SQL - a recursive CTE has
    to carry the path as an array and check it for cycles by hand.  Cypher
    does it in one line, and it is the strongest single argument for the graph
    store.

    ## Three things this used to get wrong

    It followed CALLS and nothing else.  A change reaches another member
    through nine kinds of edge, and the other eight were invisible: a program
    that reaches a file only through an OVRDBF in a CL driver, or a copybook
    two members share, had no route at all.  `edges="all"` follows all nine
    and names each hop; `edges="calls"` stays the default because "how does
    control get there" is the common question and mixing a rebuild edge into
    that answer buries it.

    It returned ONE path.  `shortestPath` picks an arbitrary one of the
    shortest, so two programs joined three different ways reported one, and
    which one depended on the planner.  Reading that as "the" route is exactly
    the bias a single answer invites, and it is the same failure the tie
    reporting in `_rank_candidates` exists to prevent.  `allShortestPaths`
    returns all of them.

    Its hop limit was invisible.  It read `query.max_hops` from the config
    and neither took an argument nor said what the limit was, so "no path"
    could mean "no path" or "not within a bound you cannot see".  `max_hops`
    is now an argument and the caller is told what was used.

    ## Why the types go in the pattern

    `allShortestPaths` finds the shortest path and a WHERE over
    `relationships(p)` filters afterwards, so a filtered route that is not the
    globally shortest one is dropped rather than found.  Putting the types in
    the pattern makes the search itself respect them.  The names come from
    MEMBER_EDGES and nowhere else, so there is no user text in the string.
    """
    want = (list(MEMBER_EDGES) if str(edges).strip().lower() == "all"
            else list(CALL_EDGES))
    # `:A|B|C`, not `:A|:B|:C` - Neo4j 5 rejects the repeated colon when
    # the pattern also has a variable length, and the error names the
    # form it wants.
    pattern = ":" + "|".join(want)
    h = _hops(max_hops)
    k = max(1, min(int(max_paths or 8), 25))
    rows = driver.read(f"""
        MATCH (a:Member {{name: $frm}}), (b:Member {{name: $to}})
        MATCH p = allShortestPaths((a)-[{pattern}*1..{h}]->(b))
        RETURN [n IN nodes(p) | n.name] AS route,
               [r IN relationships(p) | type(r)] AS kinds,
               length(p) AS hops
        // hops alone ties by construction - allShortestPaths returns routes
        // of the SAME length, so ordering by it leaves every row tied and
        // the LIMIT then keeps an arbitrary subset. Ordering by the route
        // and the edge types makes which ones survive reproducible.
        ORDER BY hops, route, kinds
        LIMIT {k}
    """, {"frm": frm.strip().upper(), "to": to.strip().upper()})
    for r in rows:
        r["max_hops"] = h
        r["edges"] = "all" if want != list(CALL_EDGES) else "calls"
    return rows


# ---------------------------------------------------------------------------
# files, and the logical-file bridge
# ---------------------------------------------------------------------------

def who_touches_file(file: str, mode: Optional[str] = None) -> List[Dict[str, Any]]:
    """Programs that open a file, including through any logical over it.

    The `via` column is the point.  A program reaching CUSMAS through CUSMASL3
    never names CUSMAS, so a text search misses it; here the logical is
    followed and reported, so the answer says how it got there.
    """
    where = "AND o.mode = $mode" if mode in ("I", "O", "U") else ""
    return driver.read(f"""
        MATCH (target:Member {{name: $file}})
        OPTIONAL MATCH (lf:Member)-[:BASED_ON]->(target)
        WITH target, collect(DISTINCT lf) AS logicals
        UNWIND ([target] + logicals) AS f
        MATCH (p:Member)-[o:OPENS]->(f)
        WHERE p.stub IS NULL {where}
        RETURN DISTINCT p.name AS program, p.member_type AS member_type,
               p.library AS library, f.name AS via,
               (f.name <> $file) AS through_logical,
               o.mode AS mode, o.verbs AS verbs,
               o.lines AS lines, o.n AS n, o.confidence AS confidence
        ORDER BY through_logical, program
    """, {"file": file.strip().upper(), "mode": mode})


def resolve_overrides(program: str) -> List[Dict[str, Any]]:
    """Which physical file a program actually opens.

    Two edges, joined: the program OPENS a label, and some CL driver
    OVERRIDES_TO a real file for that label.  `library` on the override is the
    answer people want and it is `inferred` whenever it came from a CL
    variable, which in this codebase is always.
    """
    return driver.read("""
        MATCH (p:Member {name: $program})-[o:OPENS]->(named:Member)
        OPTIONAL MATCH (cl:Member)-[ov:OVERRIDES_TO]->(actual:Member)
        WHERE ov.logical_name = named.name
        RETURN named.name AS declared, o.mode AS mode, o.verbs AS verbs,
               cl.name AS overridden_by, actual.name AS actual_file,
               ov.library AS library, ov.share AS share,
               coalesce(ov.confidence, 'unresolved') AS confidence,
               ov.lines AS override_lines, o.lines AS open_lines
        ORDER BY declared
    """, {"program": program.strip().upper()})


# ---------------------------------------------------------------------------
# fields
# ---------------------------------------------------------------------------

def where_is_field_used(field: str,
                        direction: str = "any") -> List[Dict[str, Any]]:
    """Programs reading or writing a field.

    Returns programs AND occurrence counts separately, because conflating them
    is what made the old tool report "493 write it" when 164 programs did.
    """
    rel = {"read": "READS_FIELD", "write": "WRITES_FIELD"}.get(
        direction, "READS_FIELD|WRITES_FIELD")
    return driver.read(f"""
        MATCH (p:Member)-[u:{rel}]->(f:Field {{name: $field}})
        WHERE p.stub IS NULL
        RETURN p.name AS program, p.library AS library,
               type(u) AS direction, u.file AS file,
               u.paragraphs AS paragraphs, u.lines AS lines,
               u.n AS n, u.confidence AS confidence
        ORDER BY direction DESC, program
    """, {"field": field.strip().upper()})


def field_definitions(field: str) -> List[Dict[str, Any]]:
    return driver.read("""
        MATCH (owner:Member)-[d:DEFINES_FIELD]->(f:Field {name: $field})
        WHERE owner.stub IS NULL
        RETURN owner.name AS owner, d.owner_kind AS owner_kind,
               d.dtype AS dtype, d.length AS length, d.decimals AS decimals,
               d.text AS text, d.is_key AS is_key, d.values_json AS values_json,
               d.line AS line
        ORDER BY owner_kind, owner
    """, {"field": field.strip().upper()})


# ---------------------------------------------------------------------------
# paragraph structure
# ---------------------------------------------------------------------------

def program_structure(member: str, depth: int = 4) -> List[Dict[str, Any]]:
    h = _hops(depth)
    return driver.read(f"""
        MATCH (m:Member {{name: $member}})-[:HAS_PARAGRAPH]->(entry:Paragraph)
        WHERE NOT ()-[:PERFORMS]->(entry)
        MATCH p = (entry)-[:PERFORMS*0..{h}]->(para:Paragraph)
        WITH para, min(length(p)) AS level
        RETURN para.name AS paragraph, para.kind AS kind, para.line AS line,
               para.end_line AS end_line, level
        ORDER BY level, line
    """, {"member": member.strip().upper()})


# ---------------------------------------------------------------------------
# blast radius - the one query the whole store is for
# ---------------------------------------------------------------------------

def blast_radius(name: str, depth: int = 3) -> Dict[str, Any]:
    """Everything a change to `name` could reach, in one round trip.

    In SQLite this is six separate queries the caller has to stitch together.
    Here it is one, and the stitching is the database's job - which is the
    argument for the graph that survives contact with measurement.
    """
    # One CALL subquery per dependency, NOT seven OPTIONAL MATCHes.
    #
    # Chained OPTIONAL MATCH multiplies: every clause joins against the rows the
    # previous ones produced, and only the final count(DISTINCT ...) collapses
    # it. For CUSMAS that is 1,113 copiers x 7 logicals x 3 writers x 1,848
    # readers x 921 overrides = 3.9e10 intermediate rows, and the query never
    # returns - measured, not estimated. ORD100 builds 19 rows, which is why it
    # looked fine.
    #
    # A CALL subquery aggregates on its own and returns one row, so the cost is
    # the SUM of the seven rather than the product.
    h = _hops(depth)
    rows = driver.read(f"""
        MATCH (m:Member {{name: $name}})
        CALL {{ WITH m
            MATCH (caller:Member)-[:CALLS*1..{h}]->(m)
            WHERE caller.stub IS NULL
            RETURN count(DISTINCT caller) AS callers }}
        CALL {{ WITH m
            MATCH (m)-[:CALLS*1..{h}]->(callee:Member)
            WHERE callee.stub IS NULL
            RETURN count(DISTINCT callee) AS callees }}
        CALL {{ WITH m
            MATCH (copier:Member)-[:COPIES]->(m)
            RETURN count(DISTINCT copier) AS recompile }}
        CALL {{ WITH m
            MATCH (lf:Member)-[:BASED_ON]->(m)
            RETURN count(DISTINCT lf) AS logicals,
                   collect(DISTINCT lf.name)[0..20] AS logical_names }}
        // The OPEN mode is not the whole answer, and this is the layer
        // where getting it wrong is expensive.
        //
        // A program that opens INPUT and issues REWRITE can still change the
        // data - invalid COBOL, but it is in this codebase, and schema.sql
        // says any query asking "what can change this file" must read both
        // columns or it drops 294 programs. This clause read only `mode`, so
        // blast_radius reported 3 writers for CUSMAS where who_touches_file
        // reported 5, including CUS950, which opens `read` and issues
        // READ REWRITE WRITE. An undercount here is an underestimate of a
        // change, which is exactly what this tool exists to prevent.
        //
        // Same predicate as tools._CAN_CHANGE, deliberately: two tools
        // answering "what can change this" must not disagree.
        CALL {{ WITH m
            MATCH (writer:Member)-[w:OPENS]->(m)
            WHERE w.mode IN ['O', 'U']
               OR w.verbs CONTAINS 'WRITE' OR w.verbs CONTAINS 'DELETE'
            RETURN count(DISTINCT writer) AS writers,
                   collect(DISTINCT writer.name)[0..20] AS writer_names }}
        CALL {{ WITH m
            MATCH (reader:Member)-[rd:OPENS]->(m)
            WHERE rd.mode = 'I'
              AND NOT (rd.verbs CONTAINS 'WRITE'
                       OR rd.verbs CONTAINS 'DELETE')
            RETURN count(DISTINCT reader) AS readers }}
        CALL {{ WITH m
            MATCH (cl:Member)-[:OVERRIDES_TO]->(m)
            RETURN count(DISTINCT cl) AS overrides,
                   collect(DISTINCT cl.name)[0..20] AS override_names }}
        RETURN callers, callees, recompile, logicals, writers, readers,
               overrides, writer_names, logical_names, override_names
    """, {"name": name.strip().upper()}, cap=1)
    return rows[0] if rows else {}


# ---------------------------------------------------------------------------
# find_object
# ---------------------------------------------------------------------------

def find_object(name: str) -> List[Dict[str, Any]]:
    """A member, with the degree counts the tool prints.

    Returns one row per (name, member_type, library) because the name alone is
    not unique - and saying so is better than picking one.
    """
    return driver.read("""
        MATCH (m:Member {name: $name}) WHERE m.stub IS NULL
        OPTIONAL MATCH (m)-[:OPENS]->(f:Member)
        OPTIONAL MATCH (caller:Member)-[:CALLS]->(m)
        OPTIONAL MATCH (m)-[:CALLS]->(callee:Member)
        OPTIONAL MATCH (m)-[:COPIES]->(cb:Member)
        OPTIONAL MATCH (m)-[:READS_FIELD|WRITES_FIELD]->(fl:Field)
        RETURN m.name AS name, m.member_type AS member_type,
               m.library AS library, m.language AS language,
               m.subsystem AS subsystem, m.tier AS tier, m.depth AS depth,
               m.path AS path, m.lines AS lines,
               m.parse_status AS parse_status, m.based_on AS based_on,
               m.unresolved AS unresolved, m.disagreements AS disagreements,
               count(DISTINCT f) AS opens, count(DISTINCT caller) AS called_by,
               count(DISTINCT callee) AS calls, count(DISTINCT cb) AS copies,
               count(DISTINCT fl) AS fields
        ORDER BY member_type, library
    """, {"name": name.strip().upper()})


# ---------------------------------------------------------------------------
# menus - the entry layer
# ---------------------------------------------------------------------------

#: How far past the menu's own target to keep following CALLs.
#:
#: A menu option does not run a COBOL program.  It runs a CL driver, and the
#: driver calls the program: APMAIN option 3 "Audit recalculation" runs
#: SEC407C, and SEC407C calls SEC407.  That is the ordinary shape on this
#: platform, not an edge case.
#:
#: So the path has to change edge type once.  Matching MENU_OPTION alone
#: reached 704 members; allowing CALLS after it reaches 1,424, and the 720 in
#: between were being told "no menu reaches this" about programs a user
#: reaches by pressing 3.
#:
#: 2 rather than more because a driver that calls a program that calls a
#: program is still one click, but the answer stops being a route somebody can
#: follow, and `who_calls` is the better tool for that question.
CALL_HOPS = 2


def menu_path_to(name: str, depth: int = 4) -> List[Dict[str, Any]]:
    """How a user reaches this program from a menu.

    Only answerable now that menu_option is indexed, and it is the question a
    business user actually asks: "where do I click to get here".

    Two edge types, in order: MENU_OPTION to get from a menu to whatever the
    option runs, then CALLS to get from that to the program asked about.  See
    CALL_HOPS for why the second half exists.
    """
    h = _hops(depth)
    return driver.read(f"""
        MATCH p = (menu:Member)-[opts:MENU_OPTION*1..{h}]->(entry:Member)
        MATCH q = (entry)-[:CALLS*0..{CALL_HOPS}]->(target:Member {{name: $name}})
        // Do not walk a MENU's calls.  A menu member carries a CALLS
        // edge to each of its option targets as well as a MENU_OPTION
        // edge, so following CALLS out of a menu re-walks the options
        // under a second name.  That produced routes like "option 11,
        // Return to the main menu" arriving at a program, which is
        // true only if you then press the option that actually runs
        // it - and the route printed did not include that keystroke.
        // Zero call hops is always allowed, so asking about a menu or
        // about an option's own target still answers.
        WHERE length(q) = 0
           OR NOT entry.member_type IN ['MNUCMD', 'MNUDDS']
        WITH menu.name AS entry_menu,
             [r IN relationships(p) | r.option] AS options,
             [r IN relationships(p) | r.label] AS labels,
             entry.name AS runs,
             [n IN nodes(q) | n.name] AS via,
             length(p) AS hops,
             length(q) AS call_hops
        // DISTINCT because a menu is two members - the MNUDDS screen and the
        // MNUCMD command list - so the same route arrives twice.
        RETURN DISTINCT entry_menu, options, labels, runs, via, hops, call_hops
        ORDER BY hops, call_hops, entry_menu
    """, {"name": name.strip().upper()})


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# full text
# ---------------------------------------------------------------------------

def search_text(terms: Sequence[str], limit: int = 25) -> List[Dict[str, Any]]:
    """Rank members by the English in them.  Lucene, via kb.graph.fulltext.

    Signature matches `kb.server.tools._search_text_sqlite` so `backend.rows`
    can route between them, and the columns are chosen so `compare` diffs
    something meaningful: both return `name` and `kind`, and the score column is
    named differently on purpose - FTS5's `rank` is a NEGATIVE bm25 and Lucene's
    `score` is a positive one.  They are not on the same scale and comparing
    them would only ever report a false disagreement, so the intersection of
    columns is `{name, kind}` and that is exactly the right comparison.

    Callers must rank by POSITION, not by the value.  `_rank_candidates` does.
    """
    from kb.graph import fulltext
    q = fulltext.lucene_query(list(terms))
    if not q:
        return []
    fulltext.ensure_ready()
    from kb.graph import model
    return driver.read(
        """
        CALL db.index.fulltext.queryNodes($index, $q) YIELD node, score
        WHERE node.stub IS NULL
        RETURN node.name AS name, node.member_type AS kind, score
        ORDER BY score DESC, name
        LIMIT $limit
        """,
        {"index": model.FULLTEXT[0].name if model.FULLTEXT
                   else "ap_member_ft", "q": q, "limit": int(limit)},
        cap=max(1, int(limit)))
