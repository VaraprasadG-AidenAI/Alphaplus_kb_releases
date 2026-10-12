"""
The map from 23 SQLite tables to a property graph.  Data only - no I/O.

`kb/graph/load.py` reads this to build the graph, `kb/graph/queries.py` reads it
to query the graph, and `kb/graph/schema.py` reads it to create the constraints.
One definition, three consumers, so a column renamed here cannot leave the
loader and the queries disagreeing.

## Three decisions the rest of the module follows from

**Identity is the composite key, not the name.**  `object`'s primary key is
`(name, member_type, library)`, because the same member name genuinely appears
in more than one library and a PF and its LF share a name in some shops.  So
every Member node carries `uid = name|member_type|library` and that is what the
constraint is on.  Matching on `name` alone is still supported - it is what a
ticket gives you - but it can return more than one node and the queries say so.

**Repeats collapse into list properties.**  A program reading the same field
twenty times is one relationship with twenty line numbers, not twenty
relationships.  SQLite already stores it that way after the field_usage
collapse; the graph keeps the same shape so the two stores stay comparable.

**Every node and relationship carries `src`.**  The member it was extracted
from.  Without it an incremental refresh cannot find what to delete when a
member changes, and the graph silently accumulates stale edges - the failure
that has no error message.  `update.py` deletes by member name in SQLite; the
graph equivalent is `WHERE r.src = $member`, and it only works if every write
sets it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: Separator inside a composite uid.  A pipe cannot occur in an AS/400 object
#: name (10 chars, A-Z 0-9 $ # @) so it is safe as a delimiter.
SEP = "|"


def uid(name: str, member_type: str = "", library: str = "") -> str:
    """The canonical node id.  Order matches the SQLite primary key."""
    return f"{name}{SEP}{member_type}{SEP}{library}"


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeSpec:
    label: str
    #: SQLite table it comes from
    table: str
    #: columns that form the identity, in uid order
    key: Sequence[str]
    #: remaining columns carried as properties
    props: Sequence[str]
    #: SELECT used to read it out of SQLite
    select: str
    #: what this node is, for `doctor` and the docs
    note: str
    #: properties worth their own index because a tool filters on them
    indexed: Sequence[str] = ()


#: The join that makes the graph self-describing, as ONE definition.
#:
#: `sha256` lives on member_hash, not on object, and diffing it is the whole
#: of incremental refresh - so this join is load-bearing.  It was written out
#: twice: here, and again by hand in `kb.graph.update.sqlite_hashes`.  Two
#: copies of it meant a renamed column could leave the LOADER working and the
#: DIFF reporting every member "unhashed" - which reads as "nothing to do", the
#: one wrong answer that looks like success.  One string, two readers.
#:
#: LEFT JOIN because a member with no hash row is still a member; it just
#: cannot be diffed.
MEMBER_HASH_JOIN = """
              FROM object o
              LEFT JOIN member_hash h
                     ON h.name = o.name
                    AND h.member_type = o.member_type
                    AND COALESCE(h.library,'') = COALESCE(o.library,'')
"""


NODES: Tuple[NodeSpec, ...] = (
    NodeSpec(
        label="Member", table="object",
        key=("name", "member_type", "library"),
        # `based_on` is here as well as being the BASED_ON edge.  The edge is
        # what you traverse; the property is what `find_object` prints as
        # "built over X".  It was missing, so that line never appeared and the
        # Cypher read `m.based_on` as null on every member - a silent gap, since
        # a logical file with no parent looks exactly the same.
        props=("language", "src_file", "subsystem", "tier", "depth",
               "path", "lines", "parse_status", "notes", "sha256", "based_on"),
        # sha256 comes from member_hash, not from object, and it is what makes
        # the graph self-describing: `kb.graph.update` diffs the hash it loaded
        # against the hash facts.db now holds, so an incremental refresh needs
        # no bookkeeping outside the graph.  Without it the only way to know
        # what changed is for a human to say, and a human who forgets leaves
        # stale edges behind with no error - the failure this package is
        # written against.  LEFT JOIN because a member with no hash row is
        # still a member; it just cannot be diffed.
        select="""
            SELECT o.name, o.member_type, o.library, o.language, o.src_file,
                   o.subsystem, o.tier, o.depth, o.path, o.lines,
                   o.parse_status, o.notes, COALESCE(o.based_on, '') AS based_on,
                   COALESCE(h.sha256, '') AS sha256
        """ + MEMBER_HASH_JOIN,
        indexed=("name", "member_type", "library", "subsystem", "tier", "language"),
        note="one per source member - programs, files, copybooks, commands, menus",
    ),
    NodeSpec(
        label="Field", table="field",
        key=("name",),
        props=(),
        select="SELECT DISTINCT name FROM field",
        indexed=("name",),
        note="a field name, shared across every file that defines it",
    ),
    NodeSpec(
        label="Paragraph", table="paragraph",
        key=("member", "name"),
        props=("member_type", "kind", "line", "end_line"),
        select="""
            SELECT member, name, MIN(member_type) AS member_type,
                   MIN(kind) AS kind, MIN(line) AS line, MAX(end_line) AS end_line
              FROM paragraph GROUP BY member, name
        """,
        indexed=("member", "name"),
        note="a COBOL paragraph, with its line range",
    ),
    NodeSpec(
        label="Format", table="format",
        key=("device_file", "name"),
        props=("kind", "keywords", "line"),
        select="""
            SELECT device_file, name, MIN(kind) AS kind,
                   MIN(keywords) AS keywords, MIN(line) AS line
              FROM format GROUP BY device_file, name
        """,
        indexed=("device_file", "name"),
        note="a record format on a display or printer file",
    ),
    NodeSpec(
        label="Message", table="message",
        key=("msgid",),
        props=("text", "second_level"),
        select="""
            SELECT msgid, MIN(text) AS text, MIN(second_level) AS second_level
              FROM message GROUP BY msgid
        """,
        indexed=("msgid",),
        note="a message id and its text",
    ),
    NodeSpec(
        label="DataArea", table="data_area",
        key=("name",),
        props=(),
        select="SELECT DISTINCT name FROM data_area",
        indexed=("name",),
        note="a data area - often what decides which library gets opened",
    ),
    NodeSpec(
        label="Indicator", table="indicator",
        key=("number",),
        props=(),
        select="SELECT DISTINCT number FROM indicator",
        indexed=("number",),
        note="an indicator number - the only thing linking CA03 in DDS to IN03 in COBOL",
    ),
    NodeSpec(
        label="Library", table="object",
        key=("library",),
        props=(),
        select="SELECT DISTINCT library FROM object WHERE library <> ''",
        indexed=("library",),
        note="a source library - APPALPH, SERV, SERVPLUS, ALPH",
    ),
)


# ---------------------------------------------------------------------------
# relationships
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EdgeSpec:
    rel: str
    table: str
    #: (label, uid columns) for the start node
    start: Tuple[str, Sequence[str]]
    end: Tuple[str, Sequence[str]]
    #: columns carried as properties
    props: Sequence[str]
    #: the column holding the member this fact was extracted from, for `src`
    src_col: str
    select: str
    note: str
    #: True when the end node may legitimately not exist - an unresolved
    #: target: a CALL to a program outside the extract, a COPY of a copybook
    #: that is not here, an EXEC SQL naming a table we do not hold.
    #:
    #: DESCRIPTIVE, NOT ENFORCED - and worth being straight about, because the
    #: wording here used to imply otherwise.  The loader creates a stub for the
    #: end of EVERY edge whether this is set or not, which is correct: Rule 2
    #: says never drop what you cannot resolve, so dropping the edge when the
    #: flag is false would be strictly worse than keeping it.  Nothing reads
    #: this field.
    #:
    #: What it is FOR is the check that does not exist yet: a stub at the end of
    #: an edge where this is False means a member we should have had is missing,
    #: which is a finding; where it is True the stub is expected. Today that
    #: distinction lives only in a human reading `--verify`.
    end_may_be_missing: bool = False
    #: True when the end column holds a JSON LIST rather than one name, so one
    #: row becomes zero, one or several edges.  sql_usage.tables_json is the
    #: only case: 431 of its 1,318 rows name no table at all (SET, COMMIT,
    #: DECLARE), and those are facts but not edges.  `--verify` needs to know,
    #: or it reports the shortfall as data loss.
    end_is_json_list: bool = False
    #: Properties that belong in the MERGE PATTERN, not in SET.
    #:
    #: Without these, two SQLite rows that differ only in `mode` or `file`
    #: merge into one relationship and the second silently overwrites the
    #: first.  That cost 4,701 WRITES_FIELD rows on the first load and showed
    #: up only in `--verify` - which is exactly why --verify exists.
    key_props: Sequence[str] = ()


EDGES: Tuple[EdgeSpec, ...] = (
    # ---- program to program -------------------------------------------------
    EdgeSpec(
        rel="CALLS", table="call_edge",
        start=("Member", ("caller",)), end=("Member", ("callee",)),
        props=("kind", "lines", "n", "confidence", "variable", "paragraphs"),
        src_col="caller",
        select="""
            SELECT caller, callee, MIN(kind) AS kind,
                   group_concat(line ORDER BY line) AS lines, COUNT(*) AS n,
                   MIN(confidence) AS confidence,
                   MIN(COALESCE(variable,'')) AS variable,
                   group_concat(DISTINCT COALESCE(paragraph,'')) AS paragraphs
              FROM call_edge GROUP BY caller, callee
        """,
        end_may_be_missing=True,
        note="one program calls another; variable set when the target came from a variable",
    ),
    # ---- program to file ----------------------------------------------------
    EdgeSpec(
        rel="OPENS", table="file_usage",
        start=("Member", ("program",)), end=("Member", ("file",)),
        props=("mode", "verbs", "device", "lines", "n", "confidence"),
        src_col="program",
        select="""
            SELECT program, file, mode, MIN(verbs) AS verbs,
                   MIN(device) AS device, group_concat(line ORDER BY line) AS lines,
                   COUNT(*) AS n, MIN(confidence) AS confidence
              FROM file_usage GROUP BY program, file, mode
        """,
        key_props=("mode",),
        end_may_be_missing=True,
        note="a program opens a file, with the mode and the I/O verbs",
    ),
    # ---- the logical-file bridge -------------------------------------------
    EdgeSpec(
        rel="BASED_ON", table="object",
        start=("Member", ("name",)), end=("Member", ("based_on",)),
        props=(),
        src_col="name",
        select="SELECT name, based_on FROM object WHERE based_on <> ''",
        end_may_be_missing=True,
        note="a logical file over its physical - the edge a text search cannot see",
    ),
    # ---- compile-time inclusion -------------------------------------------
    EdgeSpec(
        rel="COPIES", table="copy_edge",
        start=("Member", ("program",)), end=("Member", ("copybook",)),
        props=("kind", "resolved_from", "replacing", "lines", "n", "confidence"),
        src_col="program",
        select="""
            SELECT program, copybook, MIN(kind) AS kind,
                   MIN(resolved_from) AS resolved_from,
                   MAX(replacing) AS replacing,
                   group_concat(line ORDER BY line) AS lines, COUNT(*) AS n,
                   MIN(confidence) AS confidence
              FROM copy_edge GROUP BY program, copybook
        """,
        end_may_be_missing=True,
        note="a member copies a copybook or a DDS layout",
    ),
    # ---- the indirection layer --------------------------------------------
    EdgeSpec(
        rel="OVERRIDES_TO", table="override",
        start=("Member", ("cl_program",)), end=("Member", ("target_file",)),
        props=("logical_name", "library", "share", "kind", "lines", "n", "confidence"),
        src_col="cl_program",
        select="""
            SELECT cl_program, target_file, MIN(logical_name) AS logical_name,
                   MIN(library) AS library, MIN(share) AS share,
                   MIN(kind) AS kind, group_concat(line ORDER BY line) AS lines,
                   COUNT(*) AS n, MIN(confidence) AS confidence
              FROM override GROUP BY cl_program, target_file
        """,
        end_may_be_missing=True,
        note="a CL driver redirects a file; library is inferred when it came from a variable",
    ),
    EdgeSpec(
        rel="RESOLVES_TO", table="file_resolved",
        start=("Member", ("program",)), end=("Member", ("physical_file",)),
        props=("logical_file", "library", "via", "hops", "confidence",
               "evidence"),
        src_col="program",
        select="""
            SELECT program, physical_file, logical_file, via,
                   MIN(COALESCE(library,'')) AS library,
                   MIN(hops) AS hops, MIN(confidence) AS confidence,
                   group_concat(DISTINCT COALESCE(evidence,'')) AS evidence
              FROM file_resolved
             GROUP BY program, physical_file, logical_file, via
        """,
        # NOT optional.  The schema comment on file_resolved says it outright:
        # "two drivers can redirect one logical name to two different
        # physicals", so (program, physical_file) is not unique.  Leave these
        # out of the MERGE pattern and the second row overwrites the first
        # exactly the way 4,701 WRITES_FIELD rows disappeared on the first
        # load - silently, and only visible in --verify.
        key_props=("logical_file", "via"),
        end_may_be_missing=True,
        note="which physical file a program REALLY opens once an OVRDBF rename "
             "is applied: the COBOL says CUSTDTA, the CL made it M1_CUSTDTA. "
             "Additive - file_usage keeps saying CUSTDTA, which is the exact "
             "fact the source states. confidence is never exact: the mapping "
             "can be certain while its applicability at the CALL is not",
    ),
    # ---- field level -------------------------------------------------------
    EdgeSpec(
        rel="READS_FIELD", table="field_usage",
        start=("Member", ("program",)), end=("Field", ("field",)),
        props=("file", "qualifier", "lines", "paragraphs", "n", "confidence"),
        src_col="program",
        select="""
            SELECT program, field, file, qualifier, lines, paragraphs, n, confidence
              FROM field_usage WHERE direction = 'read'
        """,
        key_props=("file", "qualifier"),
        note="a program reads a field - already collapsed in SQLite",
    ),
    EdgeSpec(
        rel="WRITES_FIELD", table="field_usage",
        start=("Member", ("program",)), end=("Field", ("field",)),
        props=("file", "qualifier", "lines", "paragraphs", "n", "confidence"),
        src_col="program",
        select="""
            SELECT program, field, file, qualifier, lines, paragraphs, n, confidence
              FROM field_usage WHERE direction = 'write'
        """,
        key_props=("file", "qualifier"),
        note="a program writes a field - the direction that decides blast radius",
    ),
    EdgeSpec(
        rel="DEFINES_FIELD", table="field",
        start=("Member", ("owner",)), end=("Field", ("name",)),
        props=("owner_kind", "record", "dtype", "length", "decimals", "text",
               "heading", "is_key", "level", "values_json", "line"),
        src_col="owner",
        select="""
            SELECT owner, name, MIN(owner_kind) AS owner_kind,
                   MIN(record) AS record, MIN(dtype) AS dtype,
                   MIN(length) AS length, MIN(decimals) AS decimals,
                   MIN(text) AS text, MIN(heading) AS heading,
                   MAX(is_key) AS is_key, MIN(level) AS level,
                   MIN(values_json) AS values_json, MIN(line) AS line
              FROM field GROUP BY owner, name
        """,
        note="a file or program defines a field, with its type and description",
    ),
    # ---- paragraph structure ----------------------------------------------
    EdgeSpec(
        rel="HAS_PARAGRAPH", table="paragraph",
        start=("Member", ("member",)), end=("Paragraph", ("member", "name")),
        props=("kind", "line", "end_line"),
        src_col="member",
        select="""
            SELECT member, name, MIN(kind) AS kind,
                   MIN(line) AS line, MAX(end_line) AS end_line
              FROM paragraph GROUP BY member, name
        """,
        note="a program owns a paragraph",
    ),
    EdgeSpec(
        rel="PERFORMS", table="perform_edge",
        start=("Paragraph", ("member", "from_para")),
        end=("Paragraph", ("member", "to_para")),
        props=("thru_para", "form", "is_loop", "inline", "in_loop",
               "lines", "n", "confidence"),
        src_col="member",
        select="""
            SELECT member, from_para, to_para,
                   MIN(COALESCE(thru_para,'')) AS thru_para,
                   MIN(form) AS form, MAX(is_loop) AS is_loop,
                   MAX(inline) AS inline, MAX(in_loop) AS in_loop,
                   group_concat(line ORDER BY line) AS lines, COUNT(*) AS n,
                   MIN(confidence) AS confidence
              FROM perform_edge GROUP BY member, from_para, to_para
        """,
        note="paragraph performs paragraph, with the loop flags",
    ),
    # ---- screens and reports ----------------------------------------------
    EdgeSpec(
        rel="WRITES_FORMAT", table="format_usage",
        start=("Member", ("program",)), end=("Format", ("device_file", "format_name")),
        props=("verb", "paragraphs", "lines", "n", "confidence"),
        src_col="program",
        select="""
            SELECT program, device_file, format_name, MIN(verb) AS verb,
                   group_concat(DISTINCT COALESCE(paragraph,'')) AS paragraphs,
                   group_concat(line ORDER BY line) AS lines, COUNT(*) AS n,
                   MIN(confidence) AS confidence
              FROM format_usage GROUP BY program, device_file, format_name
        """,
        note="a program writes or reads a record format",
    ),
    EdgeSpec(
        rel="HAS_FIELD", table="format_field",
        start=("Format", ("device_file", "format_name")), end=("Field", ("field",)),
        props=("usage", "row", "col", "constant", "line"),
        src_col="device_file",
        select="""
            SELECT device_file, format_name, field, MIN(usage) AS usage,
                   MIN(row) AS row, MIN(col) AS col,
                   MIN(constant) AS constant, MIN(line) AS line
              FROM format_field GROUP BY device_file, format_name, field
        """,
        note="a format carries a field, at a row and column",
    ),
    # ---- messages, data areas, indicators ---------------------------------
    EdgeSpec(
        rel="SENDS", table="message_usage",
        start=("Member", ("program",)), end=("Message", ("msgid",)),
        props=("paragraphs", "lines", "n"),
        src_col="program",
        select="""
            SELECT program, msgid,
                   group_concat(DISTINCT COALESCE(paragraph,'')) AS paragraphs,
                   group_concat(line ORDER BY line) AS lines, COUNT(*) AS n
              FROM message_usage GROUP BY program, msgid
        """,
        note="a program sends a message id",
    ),
    EdgeSpec(
        rel="USES_DTAARA", table="data_area",
        start=("Member", ("member",)), end=("DataArea", ("name",)),
        props=(),
        src_col="member",
        select="SELECT DISTINCT member, name FROM data_area",
        note="a member reads or writes a data area",
    ),
    EdgeSpec(
        rel="USES_INDICATOR", table="indicator",
        start=("Member", ("member",)), end=("Indicator", ("number",)),
        props=("roles", "purposes", "lines", "n"),
        src_col="member",
        select="""
            SELECT member, number,
                   group_concat(DISTINCT COALESCE(role,'')) AS roles,
                   group_concat(DISTINCT COALESCE(purpose,'')) AS purposes,
                   group_concat(line ORDER BY line) AS lines, COUNT(*) AS n
              FROM indicator GROUP BY member, number
        """,
        note="a member uses an indicator number - bridges DDS keys to COBOL logic",
    ),
    # ---- I/O sites ---------------------------------------------------------
    EdgeSpec(
        rel="IO_AT", table="io_site",
        start=("Member", ("member",)), end=("Member", ("file",)),
        props=("verbs", "records", "paragraphs", "lines", "n", "confidence"),
        src_col="member",
        select="""
            SELECT member, file,
                   group_concat(DISTINCT verb) AS verbs,
                   group_concat(DISTINCT COALESCE(record,'')) AS records,
                   group_concat(DISTINCT COALESCE(paragraph,'')) AS paragraphs,
                   group_concat(line ORDER BY line) AS lines, COUNT(*) AS n,
                   MIN(confidence) AS confidence
              FROM io_site GROUP BY member, file
        """,
        end_may_be_missing=True,
        note="every READ, WRITE and CHAIN site, grouped per file",
    ),
    # ---- menus -------------------------------------------------------------
    EdgeSpec(
        rel="MENU_OPTION", table="menu_option",
        start=("Member", ("menu",)), end=("Member", ("target",)),
        props=("option", "label", "command", "action", "target_library",
               "label_line", "command_line", "confidence"),
        src_col="menu",
        select="""
            SELECT menu, target, option, label, command, action,
                   target_library, label_line, command_line, confidence
              FROM menu_option WHERE target <> ''
        """,
        end_may_be_missing=True,
        note="a menu option reaches a program or another menu - the entry layer",
    ),
    # ---- library membership ------------------------------------------------
    EdgeSpec(
        rel="IN_LIBRARY", table="object",
        start=("Member", ("name",)), end=("Library", ("library",)),
        props=(),
        src_col="name",
        select="SELECT name, library FROM object WHERE library <> ''",
        note="which source library a member lives in",
    ),
    # ---- embedded SQL ------------------------------------------------------
    EdgeSpec(
        rel="EXEC_SQL", table="sql_usage",
        start=("Member", ("program",)), end=("Member", ("table_name",)),
        props=("verbs", "paragraphs", "lines", "n", "confidence"),
        src_col="program",
        select="""
            SELECT program, tables_json AS table_name,
                   group_concat(DISTINCT verb) AS verbs,
                   group_concat(DISTINCT COALESCE(paragraph,'')) AS paragraphs,
                   group_concat(line ORDER BY line) AS lines, COUNT(*) AS n,
                   MIN(confidence) AS confidence
              FROM sql_usage GROUP BY program, tables_json
        """,
        end_may_be_missing=True,
        end_is_json_list=True,
        note="embedded SQL touching a table; tables_json is a list, expanded by the loader",
    ),
)


# ---------------------------------------------------------------------------
# full text
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FullTextSpec:
    """A Lucene-backed full-text index, and where its text comes from.

    This is what replaced the claim that `search` could not move.  Neo4j's
    full-text indexes are Lucene: they return a `score`, they score with BM25,
    and they take one of 45 analyzers.  What they do NOT have is FTS5's
    `bm25(table, w1, w2, w3)` per-column weighting - so the weighting is
    expressed the Lucene way instead, with the caret boost operator on a field
    (`name:WHSMAS^5 OR searchtext:warehouse`).  Same effect, different syntax.
    """
    name: str
    label: str
    #: SQLite table the text comes from.  Named explicitly, like NodeSpec.table
    #: and EdgeSpec.table, so `kb.graph.selftest` can ask "is every table in
    #: facts.db accounted for" and get the right answer.  Without it `search`
    #: looked unmapped: it had been removed from STAYS_IN_SQLITE because it MOVED,
    #: and nothing else recorded where it moved to.
    table: str
    #: node properties indexed, in the order Lucene sees them
    props: Sequence[str]
    #: which of those `props` a term is boosted on, and by how much
    boost: Dict[str, float]
    #: Lucene analyzer.  `english` stems and drops stopwords, which is the
    #: closest match to FTS5's `porter unicode61`.
    analyzer: str
    #: SQLite that supplies the text: name, member_type, body
    select: str
    note: str


#: RETIRED, deliberately empty. `search` text was projected onto every
#: :Member as `searchtext` and indexed by Lucene - 5.5 MB of document text held
#: in two stores and searched by two engines that stem differently, which was
#: the one standing compare-mode disagreement. Full text is not a traversal
#: question: the relational store answers `search_text`, and the graph holds
#: keys and relationships only. kb.graph.selftest now ENFORCES that - a member
#: carrying searchtext fails the `no text in graph` check.
FULLTEXT: Tuple[FullTextSpec, ...] = ()

_RETIRED_FULLTEXT = (
    FullTextSpec(
        name="ap_member_ft", label="Member", table="search",
        props=("name", "searchtext"),
        # 1.0 is deliberate, not a placeholder.  It reproduces FTS5's
        # behaviour on the `search` table, which had no column weights either,
        # so the migration can be judged on tokenisation alone.  Raising it is
        # a ranking CHANGE and must be re-measured with kb.eval.probe - see
        # `fulltext.name_boost` in alphaplus.toml.
        boost={"name": 1.0, "searchtext": 1.0},
        analyzer="english",
        select="SELECT name, kind AS member_type, body FROM search",
        note="the English the codebase already contains - titles, paragraph "
             "names, field descriptions, message text",
    ),
)


# ---------------------------------------------------------------------------
# what deliberately does NOT move
# ---------------------------------------------------------------------------

#: Tables that stay in SQLite, and why.  `doctor` prints this so nobody has to
#: guess whether a missing table is an oversight.
STAYS_IN_SQLITE: Dict[str, str] = {
    "search": (
        "the full-text index over member text. Was also copied into Neo4j as "
        "Member.searchtext + a Lucene index; retired - full text is answered "
        "by the relational store alone, see FULLTEXT above"),
    "search_summary": (
        "FTS5 over generated pages. Mappable - the Lucene caret boost replaces "
        "bm25()'s column weights - and the reason not to has now EXPIRED: the "
        "pyramid has been run and this holds rows, so there is something "
        "to index and something to measure. It stays only because the pages it "
        "indexes stay, and an index belongs beside its text. Moving it means "
        "moving `summary` too, and re-measuring route_ticket - bm25()'s float "
        "column weights (0.2, 3.0, 1.0) have no equivalent in either Lucene or "
        "tsvector, both of which offer discrete weight classes"),
    "summary": "written pages; the text belongs beside the FTS index that searches it",
    "embedding": "the record of what was embedded, by which model, at which width",
    "member_hash": "the size+mtime stat cache that drives incremental refresh",
    "build_info": "one row of provenance for the build itself",
    "unresolved": "findings, not facts - carried as properties on the member instead",
    "header_disagreement": "findings, not facts - carried as properties on the member",
    "call_candidate": "candidates for a variable call; empty in the replica",
    "calltree_name": (
        "written business names for capabilities, process groups and members. "
        "Not facts and not traversable: nothing is reached BY a name, and no "
        "question is answered by walking from one to another. The graph holds "
        "keys and relationships, so these are a projection onto members the "
        "viewer applies at render time, the same way `notes` is. They are also "
        "the one table a human edits by hand to correct a wrong name - a "
        "second copy in Neo4j would be a second thing to correct"),
}


def node_by_label(label: str) -> Optional[NodeSpec]:
    for n in NODES:
        if n.label == label:
            return n
    return None


def summary() -> str:
    """A block for `doctor` - what maps, what does not."""
    out: List[str] = ["NODES"]
    for n in NODES:
        out.append(f"  :{n.label:<10} from {n.table:<14} {n.note}")
    out.append("")
    out.append("RELATIONSHIPS")
    for e in EDGES:
        out.append(f"  :{e.rel:<14} {e.start[0]}->{e.end[0]:<10} from "
                   f"{e.table:<14} {e.note}")
    out.append("")
    out.append("FULL TEXT")
    for f in FULLTEXT:
        props = ", ".join(f.props)
        out.append(f"  {f.name:<14} :{f.label}({props})  "
                   f"analyzer={f.analyzer}  {f.note}")
    out.append("")
    out.append("STAYS IN SQLITE")
    for t, why in STAYS_IN_SQLITE.items():
        out.append(f"  {t:<20} {why}")
    return "\n".join(out)
