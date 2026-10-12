"""
Route a ticket to an area of the application, before it is routed to a member.

`trace_ticket` answers "which member" and answers it well. It cannot answer
"which part of the application am I even in", because that question is not
about members at all - it is about the written descriptions of menus, menu
areas and libraries, and until now those were reachable only by exact key
lookup.

Two retrievers over the same written pages, fused:

  sparse   FTS5 bm25() over `search_summary`, which the summariser has been
           populating all along and which nothing has ever read.
  dense    cosine over Snowflake Arctic Embed v2 vectors in Qdrant. Optional -
           absent, the sparse half answers alone and says so.

## Why this is a separate block and not another scoring pass

`_rank_candidates` sorts by `(-grade, -score, -indegree, name)` and grade
dominates absolutely: a semantic hit graded 0 cannot outrank a member the
ticket named outright, whatever its cosine. Adding cosine as a sixth bucket
would therefore be invisible on every ticket that names an identifier, and
`tools.py` argues at length against relaxing the grade order.

So routing is not mixed in. It answers a different question and gets its own
answer, with the retriever and the score named on every row - because a cosine
distance has no `member:line` to offer and Rule 2 still applies. Naming the
retriever is the honest substitute for evidence.

## Fusion is Reciprocal Rank Fusion, done here

RRF needs no normalisation between a bm25 value and a cosine distance, has one
conventional constant instead of a tuned weight, and does not depend on
Qdrant's local mode supporting sparse vectors.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: RRF's one constant.  60 is the value from the original paper and the one
#: every implementation uses; it is deliberately not tuned here, because tuning
#: it against forty hand-written tickets would be fitting the benchmark.
RRF_K = 60

#: Column weights for bm25(search_summary, scope, key, body).
#:
#: `key` is weighted up because a ticket that says "the audit menu" should find
#: APMAIN05 on its name, and `scope` down because matching the literal word
#: "menu" or "library" against the scope column is not evidence about anything -
#: every menu page would match the word "menu".
BM25_WEIGHTS = (0.2, 3.0, 1.0)

#: How many pages cosine hands to bm25 to rank.
#:
#: This is the recall ceiling of the whole tool: bm25 ranks the shortlist, so a
#: page cosine does not shortlist can never be returned, however well its words
#: match. 40 was a third of the corpus when the pyramid was expected to
#: produce ~112 pages, which makes the ceiling
#: generous at this size.
#:
#: It does NOT stay generous. On a corpus of thousands this number is the thing
#: to revisit first - either raise it, or go back to fusing two full-corpus
#: retrievers, which is what this replaced and which has no ceiling at all.
SHORTLIST = 40

#: Tokens too short or too common to be worth a MATCH term.
_STOP = {
    "the", "and", "for", "are", "not", "but", "with", "from", "that", "this",
    "there", "their", "them", "then", "than", "when", "what", "which", "who",
    "was", "were", "has", "have", "had", "been", "being", "would", "could",
    "should", "will", "can", "get", "got", "does", "did", "doing", "our",
    "some", "any", "all", "one", "two", "into", "out", "off", "over", "under",
    "still", "just", "only", "also", "very", "more", "most", "less",
}


@dataclass
class Hit:
    """One routed area, and how it was found."""
    scope: str
    key: str
    library: str = ""
    #: retriever -> (rank, raw score).  Both halves recorded, so the output can
    #: say "found by both" - which is the strongest routing signal there is.
    found_by: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    fused: float = 0.0
    preview: str = ""

    @property
    def retrievers(self) -> str:
        return "+".join(sorted(self.found_by))

    def why(self) -> str:
        parts = []
        for name in sorted(self.found_by):
            rank, score = self.found_by[name]
            parts.append(f"{name} #{rank + 1} ({score:.2f})")
        return ", ".join(parts)


def terms_of(text: str) -> List[str]:
    """The words worth searching for, longest first.

    Longest first because FTS5 has no term limit but a query of forty OR'd
    words matches everything and ranks nothing.
    """
    words = {w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9]{2,}", text.lower())
             if w not in _STOP}
    return sorted(words, key=lambda w: (-len(w), w))[:14]


def _tsquery(terms: Sequence[str]) -> str:
    """A to_tsquery expression - the PostgreSQL counterpart of _match_query.

    Same hazard, different syntax: a term containing `&`, `|`, `!`, `(` or `:`
    is to_tsquery OPERATOR syntax rather than a word, and one of them makes the
    whole call raise. terms_of() has already stripped punctuation, so this only
    lowercases and joins with OR; the try/except in sparse() is the backstop,
    exactly as it is for FTS5.
    """
    return " | ".join(t.lower() for t in terms)


def _match_query(terms: Sequence[str]) -> str:
    """An FTS5 MATCH expression.

    Every term is quoted. Unquoted, a word like `NEAR` or a stray `-` is FTS5
    syntax rather than a search term, and the whole query fails with an
    OperationalError that the caller can only swallow.
    """
    return " OR ".join(f'"{t}"' for t in terms)


def sparse(db: sqlite3.Connection, text: str, limit: int = 12,
           only: Optional[Sequence[Tuple[str, str, str]]] = None) -> List[Hit]:
    """bm25 over the written pages, optionally over a shortlist only.

    Reads `search_summary`, which `kb/summarise/store.py` has been writing
    since the pyramid was built and which no query had ever touched.

    `only` restricts scoring to a set of (scope, key, library) triples - the
    second stage of `route`, where cosine has already chosen the candidates and
    bm25's job is to order them rather than to find them.

    Matched on (scope, key) and NOT on library: `search_summary` carries only
    scope, key and body - there is no library column to match, and including it
    silently matched nothing at all. (scope, key) is the same identity `fuse`
    merges on, so the two stages agree on what "the same page" means.

    Written as a concatenated key so one bound list works on both stores; `||`
    and `IN` mean the same thing in each.
    """
    terms = terms_of(text)
    if not terms:
        return []
    if only is not None and not only:
        return []
    db.row_factory = sqlite3.Row

    from kb.server.tools import store_backend
    if store_backend() == "postgres":
        # tsvector instead of FTS5.  The ORDER flips - bm25() is negative and
        # better when smaller, ts_rank_cd is positive and better when larger -
        # so the score is negated to keep "ORDER BY score" meaning the same
        # thing, and to keep the Hit.score sign consistent with the SQLite half.
        #
        # BM25_WEIGHTS does NOT survive. PostgreSQL has four discrete weight
        # classes where bm25() took floats, so kb.store.ddl assigns key -> A,
        # body -> B, scope -> D, which preserves the ORDER of (0.2, 3.0, 1.0)
        # but not the magnitudes. The array below is {D,C,B,A} and is the knob
        # to tune; kb.eval.probe is what decides it, not arithmetic.
        # ts_rank has no IDF term.  bm25 does, and that difference - not the
        # column weights - is why the two stores ranked pages differently.
        # Measured on search_summary's 112 pages, for the ticket "Irish
        # customers billed the wrong VAT":
        #
        #     customers   in 17 pages   idf 1.89
        #     vat         in  5 pages   idf 3.11
        #     billed      in  2 pages   idf 4.03
        #
        # bm25 therefore weights "billed" 2.1x "customers" and picked the
        # FINMEN VAT menus; plain ts_rank_cd weighted them equally and picked
        # the customer menus, which is the wrong answer to that ticket. Four
        # different weight arrays produced IDENTICAL orderings, which is the
        # evidence that the weights were never the lever.
        #
        # So the IDF is put back: score each term separately, multiply by
        # ln(N/df), and sum. Agreement with bm25's top 4 over five tickets went
        # from 8/20 to 12/20, and "credit limit not checked before despatch"
        # went from 3/4 to 4/4.
        #
        # `?` placeholders, not named ones: kb.store.sql.to_pyformat doubles
        # every literal `%`, so `%(n)s` would arrive as `%%(n)s`.
        #
        # The df subquery runs once per term over 112 rows, which is free. On a
        # corpus of thousands of pages it would want a materialised term->df
        # table instead.
        idf_sql = (
            "WITH q AS ("
            "  SELECT term, tsq, ln(?::float / GREATEST(df, 1)) AS idf"
            "    FROM (SELECT term,"
            "                 to_tsquery('english', term) AS tsq,"
            "                 (SELECT COUNT(*) FROM search_summary z"
            "                   WHERE z.tsv @@ to_tsquery('english', term)) AS df"
            "            FROM unnest(?::text[]) AS term) x"
            "   WHERE df > 0"
            ")"
            "SELECT s.scope, s.key, s.body,"
            "       -SUM(ts_rank('{0.1, 0.2, 0.4, 1.0}', s.tsv, q.tsq)"
            "            * q.idf) AS score"
            "  FROM search_summary s JOIN q ON s.tsv @@ q.tsq"
            " GROUP BY s.scope, s.key, s.body"
            " ORDER BY score, s.key LIMIT ?")
        keys = [f"{k[0]}|{k[1]}" for k in (only or ())]
        if only is not None:
            marks = ",".join("?" for _ in keys)
            idf_sql = idf_sql.replace(
                " GROUP BY s.scope, s.key, s.body",
                f"   AND s.scope||'|'||s.key IN ({marks})"
                f" GROUP BY s.scope, s.key, s.body")
        try:
            n_pages = db.execute(
                "SELECT COUNT(*) FROM search_summary").fetchone()[0]
            args = [float(n_pages), [t.lower() for t in terms]]
            if only is not None:
                args.extend(keys)
            args.append(limit)
            rows = db.execute(idf_sql, tuple(args)).fetchall()
        except Exception:                               # noqa: BLE001
            # to_tsquery raises on a malformed term exactly as FTS5's MATCH
            # does, and for the same reason: the terms came from a ticket.
            return []
    else:
        weights = ", ".join(str(w) for w in BM25_WEIGHTS)
        restrict, keys = "", [f"{k[0]}|{k[1]}" for k in (only or ())]
        if only is not None:
            marks = ",".join("?" for _ in keys)
            restrict = f" AND scope||'|'||key IN ({marks})"
        try:
            rows = db.execute(
                f"SELECT scope, key, body, "
                f"       bm25(search_summary, {weights}) AS score "
                f"  FROM search_summary WHERE search_summary MATCH ?"
                f"{restrict} ORDER BY score LIMIT ?",
                (_match_query(terms), *keys, limit)).fetchall()
        except sqlite3.OperationalError:
            return []                   # no such table, or a query FTS5 refused

    out: List[Hit] = []
    for rank, row in enumerate(rows):
        out.append(Hit(scope=row["scope"], key=row["key"],
                       found_by={"bm25": (rank, row["score"])},
                       preview=(row["body"] or "")[:200]))
    return out


def fuse(*lists: Sequence[Hit], limit: int = 6) -> List[Hit]:
    """Reciprocal Rank Fusion over any number of ranked lists.

    A page found by both retrievers rises above one found strongly by either,
    which is the property we want: agreement between two different notions of
    similarity is better evidence than a high score from one.
    """
    merged: Dict[Tuple[str, str], Hit] = {}
    for hits in lists:
        for hit in hits:
            identity = (hit.scope, hit.key)
            kept = merged.get(identity)
            if kept is None:
                kept = Hit(scope=hit.scope, key=hit.key, library=hit.library,
                           preview=hit.preview)
                merged[identity] = kept
            kept.found_by.update(hit.found_by)
            kept.library = kept.library or hit.library
            kept.preview = kept.preview or hit.preview

    for hit in merged.values():
        hit.fused = sum(1.0 / (RRF_K + rank + 1)
                        for rank, _score in hit.found_by.values())

    # Agreement first, then fused score, then key so two runs agree.
    return sorted(merged.values(),
                  key=lambda h: (-len(h.found_by), -h.fused, h.key))[:limit]


def route(db: sqlite3.Connection, text: str, limit: int = 6,
          dense: bool = True) -> Tuple[List[Hit], str]:
    """(hits, note).  The note says what answered and what did not.

    Never raises for a missing optional dependency: routing degrades to the
    sparse half and reports that plainly, because a tool that fails when an
    extra is absent is worse than one that says what it could not use.

    ## `dense=False`

    Embedding the query is the single most expensive thing this package does:
    measured at 2,331ms of `trace_ticket`'s 2,503ms, and 10.5 seconds when
    Ollama has unloaded the model.  Everything else in a routed call is tens
    of milliseconds.

    So the caller is allowed to decline it, and `trace_ticket` does when the
    ticket already names identifiers - see there for why that is the case
    where the semantic half buys least.  This is NOT the same as the dense
    half being unavailable, and the note says which happened, because "we did
    not ask" and "we asked and could not reach it" are different facts about
    the answer.

    The bm25 path below is unchanged and already honest about being half the
    intended answer, so declining costs a weaker ordering rather than a
    missing block.
    """
    dense_hits: List[Hit] = []
    note = ""
    if not dense:
        note = ("bm25 only: the ticket named identifiers, so the semantic "
                "half was skipped to answer faster - route_ticket runs it")
    else:
        try:
            from . import semantic
            dense_hits, note = semantic.search(db, text, limit=SHORTLIST)
        except ImportError:
            note = "semantic half unavailable (kb.server.semantic missing)"

    if dense_hits:
        # STAGE 2. bm25 over the shortlist only.  Cosine has already decided
        # WHICH pages are candidates; this decides their order.
        #
        # The two are FUSED rather than bm25 simply overwriting the order, and
        # that is the part worth being careful about. bm25 can only score a
        # page that shares a word with the ticket, so ranking the shortlist by
        # bm25 alone would drop every page cosine found for its meaning - which
        # is the entire reason cosine runs first. "invoice totals disagree"
        # finds the finance pages on cosine and they share no term with it.
        #
        # So a page bm25 cannot see keeps its cosine rank and sits below the
        # pages both agree on, rather than vanishing.
        shortlist = [(h.scope, h.key, h.library) for h in dense_hits]
        sparse_hits = sparse(db, text, limit=len(shortlist), only=shortlist)
        hits = fuse(dense_hits, sparse_hits, limit=limit)
        scored = len(sparse_hits)
        stage = (f"cosine shortlisted {len(dense_hits)} page(s), bm25 scored "
                 f"{scored} of them")
        note = f"{stage}. {note}".strip() if note else stage
    else:
        # No shortlist to rank, so bm25 answers over everything. This is the
        # only path when the embedding model is unreachable, and it is why the
        # note matters: the answer is real but it is half the intended one.
        sparse_hits = sparse(db, text, limit=max(limit * 2, 12))
        if not sparse_hits:
            return [], note
        hits = fuse(sparse_hits, limit=limit)

    _enrich(db, hits)
    return hits, note


def _enrich(db: sqlite3.Connection, hits: Sequence[Hit]) -> None:
    """Attach the library each routed area belongs to."""
    db.row_factory = sqlite3.Row
    for hit in hits:
        if hit.library:
            continue
        row = db.execute("SELECT library FROM summary WHERE scope=? AND key=? "
                         "LIMIT 1", (hit.scope, hit.key)).fetchone()
        if row:
            hit.library = row["library"] or ""


def render(hits: Sequence[Hit], note: str = "") -> str:
    """The WHERE TO START block."""
    if not hits:
        if note:
            return f"  No area matched. {note}"
        return ("  No area matched. Written descriptions may not exist yet - "
                "run: python -m kb summarise")

    out = ["WHERE TO START", ""]
    for hit in hits:
        where = f"{hit.scope} {hit.key}"
        if hit.library:
            where += f"  in {hit.library}"
        out.append(f"  {where}")
        if hit.preview:
            out.append(f"      {hit.preview[:150]}")
        out.append(f"      matched by {hit.why()}")
    out.append("")
    out.append("  These are written descriptions, not facts read from source: "
               "they say where")
    out.append("  to look, not what is true. Confirm with what_it_touches or "
               "read the code.")
    if note:
        out.append(f"  {note}")
    return "\n".join(out)
