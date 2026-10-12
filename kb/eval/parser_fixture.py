"""
The parsers, against source written to exercise one rule at a time.

    python -m kb.eval.parser_fixture
    python -m kb.eval.parser_fixture -v      show every case, not just failures

`kb.index.selftest` proves the pipeline runs end to end and `coupling_fixture`
proves a set of fixed defects stays fixed. Neither asks the narrower question
this file asks: given one statement, does the parser record exactly the fact
that statement states - and nothing else?

That question matters most where the replica is silent. The generator emits
one spelling of each construct, so a parser that handles only that spelling
passes every other check in this repository and fails on the first real
member. Every case below is written from the language reference rather than
from what the generated tree happens to contain.

No member, library or field name here appears in any real codebase: the names
are deliberately shaped like source but chosen not to collide.

Dev-only: kb.eval is excluded from the build.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, List, Tuple

Case = Tuple[str, str, Callable[[], bool]]


# ===========================================================================
# Helpers
# ===========================================================================

def _parse(text: str, name: str, member_type: str, suffix: str,
           folder: str) -> Any:
    """Parse one member from text, through the real classifier and parser."""
    from kb.parse import cl, cobol, dds
    root = Path(tempfile.mkdtemp(prefix="parsefix-"))
    path = root / "SOMELIB" / folder / f"{name}.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", "\r\n").encode("cp1252"))
    parser = {"cobol": cobol, "cl": cl, "dds": dds}[
        "cl" if member_type in ("CLLE", "CLP") else
        "dds" if member_type in ("PF", "LF", "DSPF", "PRTF") else "cobol"]
    return parser.parse(text, name=name, member_type=member_type, path=path)


def cobol_src(name: str, procedure: List[str],
              data: List[str] = ()) -> str:
    """A COBOL member in fixed format, with the lines placed in area B."""
    head = [
        "       IDENTIFICATION DIVISION.",
        f"       PROGRAM-ID. {name}.",
        "       DATA DIVISION.",
        "       WORKING-STORAGE SECTION.",
    ]
    head += [f"       {line}" for line in data]
    head += [
        "       PROCEDURE DIVISION.",
        "       MAIN-LOGIC SECTION.",
    ]
    head += [f"           {line}" for line in procedure]
    head.append("           GOBACK.")
    return "\n".join(head) + "\n"


def _writes(pm: Any) -> set:
    return {u.field for u in pm.field_uses if u.direction == "write"}


def _reads(pm: Any) -> set:
    return {u.field for u in pm.field_uses if u.direction == "read"}


def _calls(pm: Any) -> List[Any]:
    return list(pm.calls)


# ===========================================================================
# The cases
# ===========================================================================

def cases() -> List[Case]:
    out: List[Case] = []

    # ---- CALL: four shapes, three answers --------------------------------
    call = _parse(cobol_src("XYZCAL", [
        "CALL 'LITPGM'   USING WS-A.",
        "CALL WS-KNOWN   USING WS-B.",
        "CALL WS-UNSET   USING WS-C.",
        "MOVE 'LATEPGM' TO WS-LATE.",
        "CALL WS-LATE    USING WS-D.",
    ], data=[
        "01  WS-KNOWN PIC X(10) VALUE 'TGTPGM'.",
        "01  WS-UNSET PIC X(10).",
        "01  WS-LATE  PIC X(10).",
    ]), "XYZCAL", "CBLLE", "CBLLE", "QCBLLESRC")
    by_conf = {}
    for edge in call.calls:
        by_conf.setdefault(edge.confidence, []).append(edge)

    out += [
        ("CALL", "a literal target is exact",
         lambda: any(e.callee == "LITPGM" and e.confidence == "exact"
                     for e in call.calls)),
        ("CALL", "a variable with a VALUE clause resolves, marked inferred",
         lambda: any(e.callee == "TGTPGM" and e.confidence == "inferred"
                     and e.variable == "WS-KNOWN" for e in call.calls)),
        ("CALL", "a variable with no VALUE is unresolved, not guessed",
         lambda: any(e.confidence == "unresolved" and not e.callee
                     and e.variable == "WS-UNSET" for e in call.calls)),
        ("CALL", "a variable filled at run time is unresolved, not inferred",
         lambda: any(e.confidence == "unresolved" and not e.callee
                     and e.variable == "WS-LATE" for e in call.calls)),
        ("CALL", "an unresolved call keeps its line, so it can be cited",
         lambda: all(e.line > 0 for e in call.calls)),
        ("CALL", "nothing is invented - four sites, four edges",
         lambda: len(call.calls) == 4),
    ]

    # ---- Figurative constants and their spellings ------------------------
    fig = _parse(cobol_src("XYZFIG", [
        "MOVE HIGH-VALUE  TO WS-A.",
        "MOVE HIGH-VALUES TO WS-B.",
        "MOVE LOW-VALUE   TO WS-C.",
        "MOVE SPACE       TO WS-D.",
        "MOVE SPACES      TO WS-E.",
        "MOVE ZERO        TO WS-F.",
        "MOVE ZEROS       TO WS-G.",
        "MOVE ZEROES      TO WS-H.",
        "MOVE ALL SPACES  TO WS-I.",
        "MOVE ALL '*'     TO WS-J.",
        "MOVE QUOTES      TO WS-K.",
    ]), "XYZFIG", "CBLLE", "CBLLE", "QCBLLESRC")
    constants = {"HIGH-VALUE", "HIGH-VALUES", "LOW-VALUE", "LOW-VALUES",
                 "SPACE", "SPACES", "ZERO", "ZEROS", "ZEROES", "QUOTE",
                 "QUOTES", "NULL", "NULLS", "ALL"}
    targets = {f"WS-{c}" for c in "ABCDEFGHIJK"}

    out += [
        ("FIGURATIVE", "no spelling is mistaken for a field",
         lambda: not (constants & (_writes(fig) | _reads(fig)))),
        ("FIGURATIVE", "every target is recorded as written, all 11",
         lambda: targets <= _writes(fig)),
        ("FIGURATIVE", "a constant source is not recorded as a read",
         lambda: not _reads(fig)),
    ]

    # ---- MOVE: the forms that are not a plain name -----------------------
    move = _parse(cobol_src("XYZMOV", [
        "MOVE WS-SRC TO WS-ONE.",
        "MOVE 'X' TO WS-TWO.",
        "MOVE WS-SRC TO WS-THREE, WS-FOUR.",
        "MOVE CORRESPONDING WS-GRP TO WS-OTHER.",
        "MOVE WS-SRC(1:4) TO WS-FIVE.",
    ]), "XYZMOV", "CBLLE", "CBLLE", "QCBLLESRC")

    out += [
        ("MOVE", "a plain move records source read and target written",
         lambda: "WS-SRC" in _reads(move) and "WS-ONE" in _writes(move)),
        ("MOVE", "a literal source is not recorded as a field read",
         lambda: "'X'" not in _reads(move) and "X" not in _reads(move)),
        ("MOVE", "a comma-separated target list records every target",
         lambda: {"WS-THREE", "WS-FOUR"} <= _writes(move)),
        ("MOVE", "CORRESPONDING does not become the source name",
         lambda: "CORRESPONDING" not in _reads(move)
         and "CORR" not in _reads(move)),
        ("MOVE", "reference modification is flagged, not lost",
         lambda: any(u.field == "WS-SRC" and u.refmod
                     for u in move.field_uses)),
    ]

    # ---- PIC and PICTURE, THRU and THROUGH -------------------------------
    words = _parse(cobol_src("XYZWRD", [
        "PERFORM A-PARA THRU Z-PARA.",
        "PERFORM B-PARA THROUGH Y-PARA.",
        "MOVE WS-LONG TO WS-SHORT.",
    ], data=[
        "01  WS-LONG    PICTURE X(30).",
        "01  WS-SHORT   PIC X(10).",
    ]), "XYZWRD", "CBLLE", "CBLLE", "QCBLLESRC")

    out += [
        ("WORDS", "PICTURE and PIC both declare a field",
         lambda: {"WS-LONG", "WS-SHORT"} <=
         {f.name for f in words.fields} or
         {"WS-LONG", "WS-SHORT"} <= (_reads(words) | _writes(words))),
        ("WORDS", "THRU and THROUGH are not taken for paragraph names",
         lambda: not ({"THRU", "THROUGH"} &
                      {p.name for p in words.paragraphs})),
    ]

    # ---- Fixed-format columns --------------------------------------------
    columns = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. XYZCOL.\n"
        "       PROCEDURE DIVISION.\n"
        "       MAIN-LOGIC SECTION.\n"
        "      *    CALL 'GHOSTPG' USING WS-X.\n"
        "           CALL 'REALPGM' USING WS-Y.\n"
        "           MOVE WS-A TO WS-B.                                          "
        "        SAR0001\n"
        "           GOBACK.\n")
    cols = _parse(columns, "XYZCOL", "CBLLE", "CBLLE", "QCBLLESRC")

    out += [
        ("COLUMNS", "a comment line - '*' in column 7 - is not executed code",
         lambda: not any(e.callee == "GHOSTPG" for e in cols.calls)),
        ("COLUMNS", "the code line beside it is still read",
         lambda: any(e.callee == "REALPGM" for e in cols.calls)),
        ("COLUMNS", "a change mark past column 72 is not part of the statement",
         lambda: "SAR0001" not in (_reads(cols) | _writes(cols))),
    ]

    # ---- File I/O --------------------------------------------------------
    io = _parse(cobol_src("XYZIO", [
        "READ XYZFIL-F.",
        "WRITE XYZFIL-R.",
        "REWRITE XYZFIL-R.",
        "DELETE XYZFIL-F.",
    ]), "XYZIO", "CBLLE", "CBLLE", "QCBLLESRC")
    sites = {(s.verb or "").upper() for s in io.io_sites}

    out += [
        ("IO", "READ is recorded",    lambda: "READ" in sites),
        ("IO", "WRITE is recorded",   lambda: "WRITE" in sites),
        ("IO", "REWRITE is recorded - it is a write, not a read",
         lambda: "REWRITE" in sites),
        ("IO", "DELETE is recorded",  lambda: "DELETE" in sites),
    ]

    # ---- CL: how far a library can be followed ---------------------------
    # An OVRDBF says which physical file a program actually opens, so a
    # library that cannot be followed is a file nobody can name. 2,064 of
    # them on the real extract, and the two cheap cases below - a variable
    # holding another variable, and a name built by concatenation - were
    # being thrown away rather than followed.
    ovr_src = "\n".join([
        "             PGM        PARM(&INLIB)",
        "             DCL        VAR(&INLIB)  TYPE(*CHAR) LEN(10)",
        "             DCL        VAR(&BASE)   TYPE(*CHAR) LEN(10) VALUE('APLUS')",
        "             DCL        VAR(&SUFFIX) TYPE(*CHAR) LEN(10) VALUE('DTA')",
        "             DCL        VAR(&DIRECT) TYPE(*CHAR) LEN(10) VALUE('PLAINLIB')",
        "             DCL        VAR(&CHAIN1) TYPE(*CHAR) LEN(10)",
        "             DCL        VAR(&CHAIN2) TYPE(*CHAR) LEN(10)",
        "             DCL        VAR(&JOINED) TYPE(*CHAR) LEN(10)",
        "             DCL        VAR(&TRIMCAT) TYPE(*CHAR) LEN(10)",
        "             OVRDBF     FILE(XYZF01) TOFILE(SOMEDTA/XYZP01)",
        "             OVRDBF     FILE(XYZF02) TOFILE(&DIRECT/XYZP02)",
        "             CHGVAR     VAR(&CHAIN1) VALUE(&DIRECT)",
        "             CHGVAR     VAR(&CHAIN2) VALUE(&CHAIN1)",
        "             OVRDBF     FILE(XYZF03) TOFILE(&CHAIN2/XYZP03)",
        "             CHGVAR     VAR(&JOINED) VALUE(&BASE *CAT &SUFFIX)",
        "             OVRDBF     FILE(XYZF04) TOFILE(&JOINED/XYZP04)",
        "             CHGVAR     VAR(&TRIMCAT) VALUE(&BASE *TCAT 'WRK')",
        "             OVRDBF     FILE(XYZF05) TOFILE(&TRIMCAT/XYZP05)",
        "             OVRDBF     FILE(XYZF06) TOFILE(&INLIB/XYZP06)",
        "             ENDPGM",
    ])
    ovr_pm = _parse(ovr_src, "XYZOVR", "CLLE", "CLLE", "QCLSRC")
    libs = {o.logical_name: (o.library or "", o.confidence)
            for o in ovr_pm.overrides}
    flagged = {u.reason.split()[2] for u in ovr_pm.unresolved
               if u.kind == "variable_library"}

    out += [
        ("OVERRIDE", "a literal library is exact",
         lambda: libs.get("XYZF01") == ("SOMEDTA", "exact")),
        ("OVERRIDE", "a variable set by DCL VALUE resolves",
         lambda: libs.get("XYZF02") == ("PLAINLIB", "inferred")),
        ("OVERRIDE", "a CHGVAR chain is followed to its end",
         lambda: libs.get("XYZF03") == ("PLAINLIB", "inferred")),
        ("OVERRIDE", "a library built with *CAT is followed",
         lambda: libs.get("XYZF04") == ("APLUSDTA", "inferred")),
        ("OVERRIDE", "*TCAT trims the left operand before joining",
         lambda: libs.get("XYZF05") == ("APLUSWRK", "inferred")),
        ("OVERRIDE", "a library that arrives as a PARM is NOT guessed",
         lambda: libs.get("XYZF06", ("x", ""))[0] == ""),
        ("OVERRIDE", "and it is reported as a finding, not dropped silently",
         lambda: "XYZP06" in flagged),
        ("OVERRIDE", "nothing that could be followed was left unflagged",
         lambda: flagged == {"XYZP06"}),
    ]

    # ---- CL: the override forms ------------------------------------------
    clsrc = (
        "             PGM\n"
        "             DCL        VAR(&DTALIB) TYPE(*CHAR) LEN(10) "
        "VALUE('SOMEDTA')\n"
        "             OVRDBF     FILE(XYZF01) TOFILE(SOMEDTA/XYZP01)\n"
        "             OVRDBF     FILE(XYZF02) TOFILE(&DTALIB/XYZP02)\n"
        "             CALL       PGM(XYZPGM) PARM('A')\n"
        "             ENDPGM\n")
    clm = _parse(clsrc, "XYZCL", "CLLE", "CLLE", "QCLSRC")
    ovr = {o.logical_name: o for o in clm.overrides}

    out += [
        ("CL", "an override with a literal library is exact",
         lambda: "XYZF01" in ovr and ovr["XYZF01"].confidence == "exact"),
        ("CL", "an override through a CL variable resolves, marked inferred",
         lambda: "XYZF02" in ovr
         and ovr["XYZF02"].confidence == "inferred"
         and ovr["XYZF02"].library == "SOMEDTA"),
        ("CL", "both name their target file",
         lambda: ovr.get("XYZF01") is not None
         and ovr["XYZF01"].target_file == "XYZP01"
         and ovr["XYZF02"].target_file == "XYZP02"),
        ("CL", "a CALL out of CL is recorded",
         lambda: any(e.callee == "XYZPGM" for e in clm.calls)),
    ]

    # ---- DDS: columns are the whole job ----------------------------------
    ddssrc = (
        "     A          R XYZREC\n"
        "     A            XYZCOD         6A         TEXT('Code')\n"
        "     A            XYZNUM         7P 0       TEXT('Number')\n"
        "     A            XYZREF    R              REFFLD(XYZFLD XYZOTH)\n"
        "     A          K XYZCOD\n")
    pf = _parse(ddssrc, "XYZPF", "PF", "PF", "QDDSSRC")
    fields = {f.name: f for f in pf.fields}

    out += [
        ("DDS", "a character field takes its length from columns 30-34",
         lambda: fields.get("XYZCOD") is not None
         and fields["XYZCOD"].length == 6
         and fields["XYZCOD"].dtype == "A"),
        ("DDS", "a packed field keeps its decimals",
         lambda: fields.get("XYZNUM") is not None
         and fields["XYZNUM"].length == 7
         and fields["XYZNUM"].dtype == "P"),
        ("DDS", "a REFFLD field is recorded even though its shape is elsewhere",
         lambda: "XYZREF" in fields),
        ("DDS", "the key is recorded",
         lambda: any(getattr(f, "is_key", 0) for f in pf.fields)),
        ("DDS", "the record format is recorded",
         lambda: any(f.record == "XYZREC" for f in pf.fields)),
    ]

    # ---- the source record prefix, in the shapes a real export writes ----
    # Six digits of sequence and six of change date in front of every line.
    # It moves every column twelve places, so DDS reads a digit where the
    # form type belongs and a command member cannot be recognised at all.
    from kb.index.build import strip_sequence_area
    from kb import config

    with_date = "\n".join([
        "000001211126     A*   a comment",
        "000002211126     A          R XYZREC",
        "000003211126     A            XYZCOD         6A",
        "000004211126     A          K XYZCOD",
    ])
    blank_date = "\n".join([
        "000001           A*   never changed since it was written",
        "000002           A          R XYZREC",
        "000003           A            XYZCOD         6A",
        "000004           A          K XYZCOD",
    ])
    cobol_seq = "\n".join([
        "000100 IDENTIFICATION DIVISION.",
        "000200 PROGRAM-ID. XYZPGM.",
        "000300 PROCEDURE DIVISION.",
        "000400     MOVE XYZ-COD TO WS-A.",
    ])
    plain = "\n".join([
        "     A          R XYZREC",
        "     A            XYZCOD         6A",
        "     A          K XYZCOD",
    ])

    dated, dated_n = strip_sequence_area(with_date, "PF")
    blank, blank_n = strip_sequence_area(blank_date, "PF")
    kept, kept_n = strip_sequence_area(cobol_seq, "CBLLE")
    untouched, untouched_n = strip_sequence_area(plain, "PF")

    out += [
        ("PREFIX", "sequence and a real date are removed",
         lambda: dated_n == 4
         and dated.split("\n")[1] == "     A          R XYZREC"),
        ("PREFIX", "a BLANK date column is removed too - the 52-member bug",
         lambda: blank_n == 4
         and blank.split("\n")[1] == "     A          R XYZREC"),
        ("PREFIX", "the form type lands in column 6, where DDS reads it",
         lambda: dated.split("\n")[1][5] == "A"
         and blank.split("\n")[1][5] == "A"),
        ("PREFIX", "COBOL's own six-digit sequence area is NOT stripped",
         lambda: kept_n == 0 and kept == cobol_seq),
        ("PREFIX", "a member with no prefix is left exactly as it was",
         lambda: untouched_n == 0 and untouched == plain),
    ]

    # ---- the source physical file says what a member is ------------------
    # On IBM i the file a member sits in IS its type. A member with no
    # extension has nothing else to go on, and the folder had been consulted
    # only as a last resort - so an SQL script in QSQLSRC was dropped.
    out += [
        ("FOLDER", "a copybook source file this map never held is recognised",
         lambda: config.source_file_type("QCBLLECPY") == "CPY"),
        ("FOLDER", "a command source file gives CMD, not the CL its text reads as",
         lambda: config.source_file_type("QCMDSRC") == "CMD"),
        ("FOLDER", "an SQL source file gives SQL",
         lambda: config.source_file_type("QSQLSRC") == "SQL"),
        ("FOLDER", "an ordinary folder is not a source physical file",
         lambda: config.source_file_type("SOMEFOLDER") == ""
         and config.source_file_type("COPY") == ""),
    ]

    return out


# ===========================================================================

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    results = []
    for layer, name, test in cases():
        try:
            ok = bool(test())
            note = ""
        except Exception as exc:                              # noqa: BLE001
            ok, note = False, f"  ({type(exc).__name__}: {exc})"
        results.append(ok)
        if args.verbose or not ok:
            print(f"{'PASS' if ok else 'FAIL'}  {layer:<11} {name}{note}")

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
