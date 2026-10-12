"""
Coupling regression fixture - a synthetic source tree that reproduces every
failure pattern found when a real ticket's reports were checked against the
source, with invented names.

    python -m kb.eval.coupling_fixture            build, index, check; exit 1 on failure
    python -m kb.eval.coupling_fixture --keep DIR keep the tree and index in DIR

Each pattern is a group of checks tagged with the fix it guards (P = parser,
T = tool, S = skill). A check that fails names the layer that regressed.

Dev-only: kb.eval is excluded from the build.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Tuple

REPO = Path(__file__).resolve().parents[2]
LIB = "CATLIB"


# ===========================================================================
# The tree
# ===========================================================================

def _cobol(program: str, procedure: List[str], data: List[str] = (),
           linkage: List[str] = (), using: str = "",
           selects: List[str] = (), fds: List[str] = ()) -> str:
    a = "       "
    b = "           "
    lines = [a + "IDENTIFICATION DIVISION.", a + f"PROGRAM-ID. {program}."]
    if selects:
        lines += [a + "ENVIRONMENT DIVISION.", a + "INPUT-OUTPUT SECTION.",
                  a + "FILE-CONTROL."] + [b + s for s in selects]
    lines += [a + "DATA DIVISION."]
    if fds:
        lines += [a + "FILE SECTION."] + [a + f for f in fds]
    lines += [a + "WORKING-STORAGE SECTION."]
    lines += [b + d for d in data]
    if linkage:
        lines += [a + "LINKAGE SECTION."] + [b + k for k in linkage]
    lines += [a + (f"PROCEDURE DIVISION USING {using}." if using
                   else "PROCEDURE DIVISION.")]
    lines += [b + p for p in procedure]
    lines += [b + "GOBACK."]
    too_long = [ln for ln in lines if len(ln) > 72]
    if too_long:
        # Columns 73-80 are not compiled; a fixture line past 72 tests the
        # fixture, not the parser.
        raise ValueError(f"fixture COBOL past column 72: {too_long[0]!r}")
    return "\n".join(lines) + "\n"


def _files() -> Dict[str, str]:
    """relative path -> content. Grows with each fixed pattern."""
    files: Dict[str, str] = {}

    # ---- P6/T1: one DDL member creates two tables ---------------------------
    files[f"{LIB}/QSQLSRC/CRTCAT01.SQL"] = """\
-- two tables in one DDL member, as the real catalogue member does
CREATE TABLE CAT_OTHER (
    OTH_KEY     CHAR(10) NOT NULL,
    OTH_TEXT    CHAR(40) NOT NULL DEFAULT ''
);
LABEL ON TABLE CAT_OTHER IS 'Catalogue - other makers';

CREATE TABLE CAT_MAIN (
    MAIN_KEY    CHAR(10) NOT NULL,
    MAIN_PRICE  DECIMAL(9, 2) NOT NULL DEFAULT 0,
    MAIN_SRC    CHAR(3) NOT NULL DEFAULT ''
);
LABEL ON TABLE CAT_MAIN IS 'Catalogue - main; the load; target';

CREATE INDEX CAT_MAIN_IX ON CAT_MAIN (MAIN_KEY);
"""
    files[f"{LIB}/QSQLSRC/TAB_ORD.SQL"] = """\
CREATE TABLE ORDERS_SQL
    FOR SYSTEM NAME ORDERSQ (
    ORDER_KEY   FOR COLUMN ORDKEY CHAR(10) NOT NULL
) RCDFMT ORDERSQR;
LABEL ON TABLE ORDERS_SQL IS 'Orders';
"""
    files[f"{LIB}/QDDSSRC/CATSTAGE.PF"] = (
        "     A          R CATSTAGER\n"
        "     A            STGKEY        10A\n"
        "     A            STGPRICE       9P 2\n")
    files[f"{LIB}/QCBLLESRC/LOADMAIN.SQLCBLLE"] = _cobol("LOADMAIN", [
        "EXEC SQL INSERT INTO CAT_MAIN (MAIN_KEY, MAIN_PRICE)",
        "    SELECT STGKEY, STGPRICE FROM CATSTAGE END-EXEC."])
    files[f"{LIB}/QCBLLESRC/LOADOTH.SQLCBLLE"] = _cobol("LOADOTH", [
        "EXEC SQL INSERT INTO CAT_OTHER (OTH_KEY)",
        "    VALUES ('X') END-EXEC."])
    files[f"{LIB}/QCBLLESRC/ORDUPD.SQLCBLLE"] = _cobol("ORDUPD", [
        "EXEC SQL UPDATE ORDERS_SQL SET ORDER_KEY = 'Y' END-EXEC."])
    # ---- P1/S7: another product swaps the live table by rename -------------
    files["XPRODLIB/QSQLSRC/SWAPCAT.SQL"] = """\
-- the other system rebuilds the catalogue and swaps it in
CREATE TABLE TMP_CAT AS (SELECT * FROM CAT_MAIN) WITH NO DATA;
INSERT INTO TMP_CAT SELECT MAIN_KEY, MAIN_PRICE, MAIN_SRC FROM CAT_MAIN;
DROP TABLE CAT_MAIN;
RENAME TABLE TMP_CAT TO CAT_MAIN;
"""
    # ---- P1: a routine's body is one statement with its own coupling ------
    files[f"{LIB}/QSQLSRC/GETCAT.SQL"] = """\
CREATE OR REPLACE PROCEDURE GET_CAT (IN P_KEY CHAR(10), OUT P_RC CHAR(2))
LANGUAGE SQL READS SQL DATA DYNAMIC RESULT SETS 1
BEGIN
    DECLARE C1 CURSOR WITH RETURN FOR
        SELECT MAIN_KEY, MAIN_PRICE FROM CAT_MAIN WHERE MAIN_KEY = P_KEY;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION SET P_RC = '99';
    IF (SELECT COUNT(*) FROM CAT_OTHER) > 0 THEN
        UPDATE ORDERS_SQL SET ORDER_KEY = P_KEY;
    END IF;
    OPEN C1;
    SET P_RC = '00';
END;
"""
    files[f"{LIB}/QCLSRC/LOADCL.CLLE"] = """\
             PGM
             CPYF       FROMFILE(QTEMP/CATSTAGE) TOFILE(*LIBL/CAT_MAIN) +
                          MBROPT(*REPLACE)
             CALL       PGM(LOADMAIN)
             ENDPGM
"""
    # ---- P2/P3: a CL passes less storage than the program's LINKAGE -------
    # The record is 10 + 5 (COMP-3) + 1000 + 2 (COMP) + 3 (OCCURS) = 1,020.
    files[f"{LIB}/QCPYSRC/LNKREQ.CPY"] = (
        "       01  LNK-REQ.\n"
        "           05  LNK-KEY        PIC X(10).\n"
        "           05  LNK-AMT        PIC S9(7)V99 COMP-3.\n"
        "           05  LNK-TEXT       PIC X(1000).\n"
        "           05  LNK-COUNT      PIC S9(4) COMP.\n"
        "           05  LNK-FLAG       PIC X OCCURS 3.\n")
    files[f"{LIB}/QCBLLESRC/RECVREQ.CBLLE"] = _cobol(
        "RECVREQ", ["MOVE SPACES TO LNK-TEXT."],
        linkage=["COPY LNKREQ.", "01  LNK-MODE           PIC X."],
        using="LNK-REQ LNK-MODE")
    # ---- T2/S3: another program copies the same LINKAGE copybook ---------
    files[f"{LIB}/QCBLLESRC/PEERREQ.CBLLE"] = _cobol(
        "PEERREQ", ["MOVE SPACES TO LNK-TEXT.", "CALL 'RECVREQ' USING LNK-REQ."],
        data=["COPY LNKREQ."])
    # ---- P4/T6: every way a program is tied to a display file -------------
    screen = ("     A                                      DSPSIZ(24 80 *DS3)\n"
              "     A          R CATHDR\n"
              "     A                                  1  2'Catalogue'\n"
              "     A            CATFLD        10A  B  3  2\n")
    for dspf in ("CATDSPA", "CATDSPB", "CATDSPD", "CATDSPE", "CATDSPF", "CATDSPZ"):
        files[f"{LIB}/QDDSSRC/{dspf}.DSPF"] = screen
    files[f"{LIB}/QCBLLESRC/SCRPGMA.CBLLE"] = _cobol(
        "SCRPGMA", [], selects=["SELECT DSPA ASSIGN TO WORKSTATION-CATDSPA-SI",
                                "    ORGANIZATION IS TRANSACTION."],
        fds=["FD  DSPA.", "01  DSPA-REC.", "    COPY DDS-ALL-FORMATS OF CATDSPA."])
    files[f"{LIB}/QCBLLESRC/SCRPGMB.CBLLE"] = _cobol(
        "SCRPGMB", [], selects=["SELECT DSPB ASSIGN WORKSTATION-CATDSPB-SI",
                                "    ORGANIZATION IS TRANSACTION."],
        fds=["FD  DSPB.", "01  DSPB-REC."])
    files[f"{LIB}/QCBLLESRC/SCRPGMD.CBLLE"] = _cobol(
        "SCRPGMD", [], data=["01  DSPD-REC.", "    COPY DDS-ALL-FORMATS OF CATDSPD."])
    files[f"{LIB}/QCBLLESRC/SCRPGME.CBLLE"] = _cobol(
        "SCRPGME", [], selects=["SELECT DSPE ASSIGN TO WORKSTATION-CATDSPE-SI",
                                "    WORKSTATION-CATDSPF."],
        fds=["FD  DSPE.", "01  DSPE-REC."])
    # ---- P5/T11: bytes read by position, never by name --------------------
    # POSKEY 1-10, POSAMT 11-15 (9P 2 packed), POSFLG 16, POSTXT 17-36.
    files[f"{LIB}/QDDSSRC/ORDPOS.PF"] = (
        "     A          R ORDPOSR\n"
        "     A            POSKEY        10A\n"
        "     A            POSAMT         9P 2\n"
        "     A            POSFLG         1A\n"
        "     A            POSTXT        20A\n")
    files[f"{LIB}/QCBLLESRC/POSPGM.CBLLE"] = _cobol(
        "POSPGM", ["IF ORDPOSR(16:1) = 'Y'", "    MOVE 'N' TO ORDPOSR(16:1)",
                   "END-IF."],
        data=["01  POS-REC.", "    COPY DDS-ALL-FORMATS OF ORDPOS."])
    files[f"{LIB}/QCBLLESRC/POSLNK.CBLLE"] = _cobol(
        "POSLNK", ["MOVE 'A' TO LNK-REQ(11:1)."],
        linkage=["COPY LNKREQ."], using="LNK-REQ")
    files[f"{LIB}/QCLSRC/POSCL.CLLE"] = (
        "             PGM\n"
        "             DCL        VAR(&OKREQ) TYPE(*CHAR) LEN(1020)\n"
        "             DCL        VAR(&LOOSE) TYPE(*CHAR) LEN(50)\n"
        "             CHGVAR     VAR(%SST(&OKREQ 1018 1)) VALUE('Y')\n"
        "             CHGVAR     VAR(&LOOSE) VALUE(%SST(&LOOSE 3 2))\n"
        "             CALL       PGM(RECVREQ) PARM(&OKREQ &LOOSE)\n"
        "             ENDPGM\n")
    # ---- P9/S9/S8: a header that says retired, against what reaches it -----
    files[f"{LIB}/QCBLLESRC/OLDPGM.CBLLE"] = (
        "      * OLDPGM - Katalogabgleich\n"
        "      * Programm ist nicht mehr aktiv, ersetzt durch LOADMAIN\n"
        + _cobol("OLDPGM", ["DISPLAY 'X'."]))
    files[f"{LIB}/QCBLLESRC/OLDLIVE.CBLLE"] = (
        "      * OLDLIVE - price refresh\n"
        "      * OBSOLETE - do not change\n"
        + _cobol("OLDLIVE", ["DISPLAY 'Y'."]))
    files[f"{LIB}/QCBLLESRC/OBSSTK.CBLLE"] = (
        "      * OBSSTK - OBSOLETE STOCK WRITE OFF\n"
        + _cobol("OBSSTK", ["DISPLAY 'Z'."]))
    files[f"{LIB}/QCLSRC/OLDCL.CLLE"] = (
        "             PGM\n             CALL       PGM(OLDLIVE)\n"
        "             ENDPGM\n")
    files[f"{LIB}/QCLSRC/TOPCL.CLLE"] = (
        "             PGM\n             CALL       PGM(OLDCL)\n"
        "             ENDPGM\n")
    # ---- P8: a copybook whose extension names its language -----------------
    files[f"{LIB}/QCPYSRC/EXTCPY.CBLLECPY"] = (
        "       01  EXT-REC.\n"
        "           05  EXT-KEY        PIC X(8).\n")
    files[f"{LIB}/QCBLLESRC/EXTUSE.CBLLE"] = _cobol(
        "EXTUSE", ["MOVE SPACES TO EXT-KEY."], data=["COPY EXTCPY."])

    # A call whose target is decided at run time. It cannot be followed by
    # anything - that is the language, not a defect - so the only question is
    # whether the tools SAY so. `callees` counts distinct names and an
    # unresolved call has none, so it reported "callees 1" for a program with
    # two call sites and mentioned nothing. A blast radius that looks complete
    # and is not is the worst answer this tool set can give.
    files[f"{LIB}/QCBLLESRC/RUNCALL.CBLLE"] = _cobol(
        "RUNCALL", [
            "CALL 'FIGCON' USING RC-A.",
            "MOVE 'DECIDED' TO RC-PGM.",
            "CALL RC-PGM   USING RC-B.",
        ], data=["01  RC-PGM PIC X(10)."])

    files[f"{LIB}/QCBLLESRC/FIGCON.CBLLE"] = _cobol(
        "FIGCON", [
            "MOVE HIGH-VALUE  TO FG-KEY.",
            "MOVE LOW-VALUE   TO FG-END.",
            "MOVE SPACE       TO FG-NAME.",
            "MOVE ZERO        TO FG-NUM.",
            "MOVE ALL ZEROES  TO FG-COUNT.",
            "MOVE ALL SPACES  TO FG-ADDR.",
            "MOVE FG-SRC      TO FG-TGT.",
        ])
    # ---- P7: a library that is read and yields nothing --------------------
    files["EMPTYLIB/QCLSRC/NOOPCL.CLLE"] = "             PGM\n             ENDPGM\n"
    files[f"{LIB}/QCLSRC/SENDCL.CLLE"] = """\
             PGM
             DCL        VAR(&REQ) TYPE(*CHAR) LEN(200)
             DCL        VAR(&OKREQ) TYPE(*CHAR) LEN(1020)
             DCL        VAR(&MODE) TYPE(*CHAR) LEN(1)
             OVRDBF     FILE(CATSTAGE) TOFILE(CAT_MAIN) LVLCHK(*NO)
             OVRDBF     FILE(CAT_OTHER) LVLCHK(*NO) SHARE(*YES)
             OVRDBF     FILE(ORDERSQ) TOFILE(*LIBL/ORDERSQ) LVLCHK(*NO)
             CALL       PGM(RECVREQ) PARM(&REQ &MODE)
             CALL       PGM(RECVREQ) PARM(&OKREQ &MODE)
             CALL       PGM(RECVREQ) PARM(&OKREQ)
             ENDPGM
"""
    return files


def build_tree(root: Path) -> None:
    for rel, text in _files().items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.replace("\n", "\r\n").encode("cp1252"))


# ===========================================================================
# Running the knowledge base against it
# ===========================================================================

def _env(tree: Path, data: Path) -> Dict[str, str]:
    env = dict(os.environ, ALPHAPLUS_SOURCE=str(tree), ALPHAPLUS_KB_DATA=str(data),
               PYTHONPATH=str(REPO))
    env["ALPHAPLUS_MEMORY_DB"] = str(data / "memory.db")
    return env


def index(tree: Path, data: Path) -> str:
    run = subprocess.run([sys.executable, "-m", "kb.index.build"], cwd=REPO,
                         env=_env(tree, data), capture_output=True, text=True)
    if run.returncode:
        raise RuntimeError(run.stdout[-2000:] + run.stderr[-2000:])
    return run.stdout


def ask(tree: Path, data: Path, calls: List[Tuple[str, dict]]) -> List[str]:
    """Tool answers, from a fresh process against the fixture index."""
    script = ("import json, sys\nfrom kb.server import tools\n"
              "calls = json.loads(sys.stdin.read())\n"
              "print(json.dumps([getattr(tools, n)(**a) for n, a in calls]))\n")
    run = subprocess.run([sys.executable, "-c", script], cwd=REPO,
                         env=_env(tree, data), input=json.dumps(calls),
                         capture_output=True, text=True)
    if run.returncode:
        raise RuntimeError(run.stderr[-3000:])
    return json.loads(run.stdout)


def query(data: Path, sql: str, *args) -> List[tuple]:
    import sqlite3
    conn = sqlite3.connect(str(data / "facts.db"))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ===========================================================================
# Checks
# ===========================================================================

Check = Tuple[str, str, Callable[[], bool]]


def checks(tree: Path, data: Path) -> List[Check]:
    names = [
        ("what_uses", {"name": "CRTCAT01"}),
        ("what_uses", {"name": "CAT_MAIN"}),
        ("who_touches_file", {"file": "CAT_OTHER", "mode": "write"}),
        ("describe_file", {"file": "CAT_MAIN"}),
        ("find_object", {"name": "CAT_MAIN"}),
        ("find_object", {"name": "CRTCAT01"}),
        ("what_uses", {"name": "TAB_ORD"}),
        ("what_uses", {"name": "ORDERS_SQL"}),
        ("resolve_files", {"name": "CAT_MAIN"}),
        ("blast_radius", {"name": "CRTCAT01"}),
        ("describe_file", {"file": "CRTCAT01"}),
        ("who_touches_file", {"file": "CAT_MAIN", "mode": "write"}),
        ("resolve_files", {"name": "CATSTAGE"}),
        ("resolve_files", {"name": "TMP_CAT"}),
        ("program_interface", {"program": "RECVREQ"}),
        ("program_interface", {"program": "SENDCL"}),
        ("resolve_overrides", {"program": "SENDCL"}),
        ("blast_radius", {"name": "RECVREQ"}),
        ("blast_radius", {"name": "PEERREQ"}),
        ("what_uses", {"name": "CATDSPA", "kind": "screens"}),
        ("what_uses", {"name": "CATDSPB", "kind": "screens"}),
        ("what_uses", {"name": "CATDSPD", "kind": "screens"}),
        ("what_uses", {"name": "CATDSPF", "kind": "screens"}),
        ("what_uses", {"name": "CATDSPZ", "kind": "screens"}),
        ("where_is_field_used", {"field": "LNK-REQ"}),
        ("search_source", {"pattern": "LNK-TEXT", "member_type": "CBLLE"}),
        ("search_source", {"pattern": "LNK-TEXT", "member_type": "CODE"}),
        ("coverage_report", {}),
        ("what_it_touches", {"name": "SCRPGME", "kind": "unresolved"}),
        ("where_is_field_used", {"field": "POSFLG"}),
        ("where_is_field_used", {"field": "LNK-FLAG"}),
        ("describe_file", {"file": "ORDPOS"}),
        ("find_object", {"name": "OLDPGM"}),
        ("who_calls", {"name": "OLDLIVE"}),
        ("who_calls", {"name": "OLDLIVE", "depth": 2}),
        ("trace_ticket", {"text": "POSFLG is wrong for QZPX orders"}),
        ("blast_radius", {"name": "RUNCALL"}),
        ("what_it_calls", {"name": "RUNCALL"}),
    ]
    a = dict(zip([f"{n}:{json.dumps(x, sort_keys=True)}" for n, x in names],
                 ask(tree, data, names)))

    def got(tool: str, **args) -> str:
        return a[f"{tool}:{json.dumps(args, sort_keys=True)}"]

    def rows(sql: str, *args) -> List[tuple]:
        return query(data, sql, *args)

    return [
        # ---- P6: tables are first-class ----------------------------------
        ("P6", "both tables of one DDL member are recorded with their labels",
         lambda: set(rows("SELECT name, label FROM sql_table WHERE ddl_member='CRTCAT01' "
                          "AND kind='table'")) == {("CAT_OTHER", "Catalogue - other makers"),
                                                   ("CAT_MAIN", "Catalogue - main; the load; target")}),
        ("P6", "an index records the table it is over",
         lambda: rows("SELECT base FROM sql_table WHERE name='CAT_MAIN_IX'") == [("CAT_MAIN",)]),
        ("P6", "FOR SYSTEM NAME is the table's name, the SQL name kept beside it",
         lambda: rows("SELECT name, sql_name FROM sql_table WHERE ddl_member='TAB_ORD'")
         == [("ORDERSQ", "ORDERS_SQL")]),
        ("P6", "each table's columns belong to that table only",
         lambda: {r[0] for r in rows("SELECT name FROM field WHERE owner='CRTCAT01' "
                                     "AND record='CAT_MAIN'")} == {"MAIN_KEY", "MAIN_PRICE", "MAIN_SRC"}),
        ("P6", "SQL writers are recorded against the table, never the DDL member",
         lambda: not rows("SELECT 1 FROM file_usage WHERE file IN ('CRTCAT01','TAB_ORD')")
         and rows("SELECT file FROM file_usage WHERE program='LOADOTH'") == [("CAT_OTHER",)]),
        ("P6", "an SQL long name maps onto the system name",
         lambda: rows("SELECT file FROM file_usage WHERE program='ORDUPD'") == [("ORDERSQ",)]),
        # ---- P0 writer chain: CPYF reads FROMFILE, writes TOFILE ------------
        ("P0", "CPYF records its TOFILE as written and its FROMFILE as read",
         lambda: set(rows("SELECT file, mode FROM file_usage WHERE program='LOADCL' "
                          "AND verbs='CPYF'")) == {("CATSTAGE", "I"), ("CAT_MAIN", "U")}),
        ("P0", "CPYF records provenance: the target was loaded from the source",
         lambda: rows("SELECT source, target FROM file_provenance WHERE cl_member='LOADCL'")
         == [("CATSTAGE", "CAT_MAIN")]),
        # ---- T1: never answer for a DDL member ------------------------------
        ("T1", "what_uses on a two-table DDL member refuses and lists both",
         lambda: "does not answer for the member" in got("what_uses", name="CRTCAT01")
         and "CAT_OTHER" in got("what_uses", name="CRTCAT01")
         and "CAT_MAIN" in got("what_uses", name="CRTCAT01")
         and "CAN CHANGE" not in got("what_uses", name="CRTCAT01")),
        ("T1", "blast_radius and describe_file refuse the same way",
         lambda: "does not answer for the member" in got("blast_radius", name="CRTCAT01")
         and "does not answer for the member" in got("describe_file", file="CRTCAT01")),
        ("T1", "what_uses on a table lists its own writers only",
         lambda: "LOADMAIN" in got("what_uses", name="CAT_MAIN")
         and "LOADCL" in got("what_uses", name="CAT_MAIN")
         and "LOADOTH" not in got("what_uses", name="CAT_MAIN")),
        ("T1", "who_touches_file on the other table names only its writer",
         lambda: "LOADOTH" in got("who_touches_file", file="CAT_OTHER", mode="write")
         and "LOADMAIN" not in got("who_touches_file", file="CAT_OTHER", mode="write")),
        ("T1", "a one-table DDL member answers for its table and says so first",
         lambda: got("what_uses", name="TAB_ORD").startswith("TAB_ORD is the DDL member")
         and "ORDUPD" in got("what_uses", name="TAB_ORD")),
        ("T1", "an SQL long name answers for the system name",
         lambda: got("what_uses", name="ORDERS_SQL").startswith("ORDERS_SQL is the SQL name of ORDERSQ")),
        ("T1", "describe_file on a table shows its label and only its columns",
         lambda: "'Catalogue - main; the load; target'" in got("describe_file", file="CAT_MAIN")
         and "MAIN_PRICE" in got("describe_file", file="CAT_MAIN")
         and "OTH_TEXT" not in got("describe_file", file="CAT_MAIN")),
        ("T1", "find_object names an SQL table and warns about its sibling",
         lambda: "is an SQL TABLE" in got("find_object", name="CAT_MAIN")
         and "also creates CAT_OTHER" in got("find_object", name="CAT_MAIN")),
        ("T1", "find_object on the DDL member lists what it creates",
         lambda: "creates  3 object(s)" in got("find_object", name="CRTCAT01")),
        ("T1", "resolve_files gives an SQL table a TEMPLATE row from its DDL member",
         lambda: "CRTCAT01:" in got("resolve_files", name="CAT_MAIN")
         and "TEMPLATE" in got("resolve_files", name="CAT_MAIN")),
        ("S2", "the writer chain to the table is complete: SQL and CPYF writers",
         lambda: all(p in got("who_touches_file", file="CAT_MAIN", mode="write")
                     for p in ("LOADMAIN", "LOADCL"))),
        # ---- P1: SQL statement by statement ---------------------------------
        ("P1", "a script's RENAME and DROP are recorded as statements",
         lambda: {"RENAME", "DROP TABLE", "INSERT", "CREATE TABLE"}
         <= {r[0] for r in rows("SELECT verb FROM sql_usage WHERE program='SWAPCAT'")}),
        ("P1", "a rename is a provenance edge: the work table becomes the live one",
         lambda: rows("SELECT source, target FROM file_provenance WHERE cl_member='SWAPCAT' "
                      "AND verb='RNMF'") == [("TMP_CAT", "CAT_MAIN")]),
        ("P1", "the swap script is a writer of the live table",
         lambda: rows("SELECT mode FROM file_usage WHERE program='SWAPCAT' AND file='CAT_MAIN' "
                      "ORDER BY mode") == [("I",), ("U",)]),
        ("P1", "INSERT ... SELECT reads its source and writes only its target",
         lambda: set(rows("SELECT file, mode FROM file_usage WHERE program='LOADMAIN'"))
         == {("CAT_MAIN", "U"), ("CATSTAGE", "I")}),
        ("P1", "the reads of an SQL statement are stored beside its tables",
         lambda: rows("SELECT reads_json FROM sql_usage WHERE program='LOADMAIN'")
         == [('["CATSTAGE"]',)]),
        ("P1", "a procedure body's semicolons do not split the procedure",
         lambda: [r[0] for r in rows("SELECT verb FROM sql_usage WHERE program='GETCAT'")]
         == ["CREATE PROCEDURE"]),
        ("P1", "a procedure reads what its cursor and IF query name, writes what it updates",
         lambda: set(rows("SELECT file, mode FROM file_usage WHERE program='GETCAT'"))
         == {("CAT_MAIN", "I"), ("CAT_OTHER", "I"), ("ORDERSQ", "U")}),
        # ---- S7/T: the load and the swap are named in resolve_files ----------
        ("S7", "the live table shows it is replaced by a rename, as high risk",
         lambda: "SWAPPED-FROM" in got("resolve_files", name="CAT_MAIN")
         and "TMP_CAT" in got("resolve_files", name="CAT_MAIN")
         and "HIGH RISK" in got("resolve_files", name="CAT_MAIN")),
        ("S7", "the work table shows it is renamed onto the live one",
         lambda: "SWAPPED-INTO" in got("resolve_files", name="TMP_CAT")),
        ("P0", "a CPYF onto a real table is a load, not a work copy",
         lambda: "LOADED-INTO" in got("resolve_files", name="CATSTAGE")
         and "COPY-TRANSIENT" not in got("resolve_files", name="CATSTAGE")
         and "LOADED-FROM" in got("resolve_files", name="CAT_MAIN")),
        # ---- P2: parameter sizes on both sides of a call -------------------
        ("P2", "a LINKAGE record copied from a copybook is sized byte for byte",
         lambda: rows("SELECT position, name, bytes, via FROM linkage_param "
                      "WHERE program='RECVREQ' ORDER BY position")
         == [(1, "LNK-REQ", 1020, "LNKREQ"), (2, "LNK-MODE", 1, "")]),
        ("P2", "a CL CALL records each argument with its declared size",
         lambda: rows("SELECT argument, bytes FROM call_argument WHERE caller='SENDCL' "
                      "ORDER BY line, position")
         == [("&REQ", 200), ("&MODE", 1), ("&OKREQ", 1020), ("&MODE", 1),
             ("&OKREQ", 1020)]),
        ("P3", "a 200-byte argument for a 1,020-byte parameter is an overrun",
         lambda: rows("SELECT position, caller_bytes, callee_bytes FROM param_mismatch "
                      "WHERE caller='SENDCL' AND kind='overrun'") == [(1, 200, 1020)]),
        ("P3", "a call passing fewer arguments than USING names is missing one",
         lambda: rows("SELECT position, callee_item FROM param_mismatch "
                      "WHERE caller='SENDCL' AND kind='missing'") == [(2, "LNK-MODE")]),
        ("P3", "a call whose sizes agree raises nothing",
         lambda: len(rows("SELECT 1 FROM param_mismatch WHERE caller='SENDCL'")) == 2),
        # ---- T3/T7: the tools say it --------------------------------------
        ("T3", "every LVLCHK(*NO) override is recorded",
         lambda: rows("SELECT COUNT(*) FROM override WHERE cl_program='SENDCL' "
                      "AND lvlchk='*NO'") == [(3,)]),
        ("T3", "resolve_overrides flags LVLCHK(*NO) as a risk",
         lambda: "RISK: 3 override(s) set LVLCHK(*NO)"
         in got("resolve_overrides", program="SENDCL")),
        ("T7", "program_interface shows the parameter sizes and the overrun",
         lambda: "1,020" in got("program_interface", program="RECVREQ")
         and "OVERRUN" in got("program_interface", program="RECVREQ")
         and "MISSING" in got("program_interface", program="RECVREQ")),
        ("T2", "blast_radius on a program lists its copybooks and who else copies them",
         lambda: "COPIES: RECVREQ copies 1 copybook(s)" in got("blast_radius", name="RECVREQ")
         and "LINKAGE record" in got("blast_radius", name="RECVREQ")),
        ("T2", "blast_radius names the linkage closure: CL and COBOL callers, mismatches",
         lambda: "2 CL caller(s)" in got("blast_radius", name="RECVREQ")
         and "1 COBOL caller(s)" in got("blast_radius", name="RECVREQ")
         and "every member copying LNKREQ" in got("blast_radius", name="RECVREQ")
         and "3 size mismatch(es)" in got("blast_radius", name="RECVREQ")),
        ("T7", "program_interface names COBOL callers it could not size-check",
         lambda: "NOT SIZE-CHECKED: 1 COBOL caller(s) (PEERREQ)"
         in got("program_interface", program="RECVREQ")),
        # ---- P4/T6: program -> display file ---------------------------------
        ("P4", "ASSIGN without TO still declares the display file",
         lambda: rows("SELECT file, device FROM file_usage WHERE program='SCRPGMB'")
         == [("CATDSPB", "workstation")]),
        ("P4", "a device name no SELECT took up is recorded as a gap",
         lambda: rows("SELECT member FROM unresolved WHERE kind='device_link_not_extracted'")
         == [("SCRPGME",)]),
        ("T6", "screen users include programs that declare the file, not only FORMAT IS",
         lambda: "SCRPGMA" in got("what_uses", name="CATDSPA", kind="screens")
         and "SCRPGMB" in got("what_uses", name="CATDSPB", kind="screens")),
        ("T6", "a program that only copies the formats is a screen user",
         lambda: "SCRPGMD" in got("what_uses", name="CATDSPD", kind="screens")),
        ("T6", "a zero backed by an extraction gap says it is not proven",
         lambda: "NOT PROVEN" in got("what_uses", name="CATDSPF", kind="screens")
         and "SCRPGME" in got("what_uses", name="CATDSPF", kind="screens")),
        ("T6", "a true zero lists what was checked",
         lambda: "NO PROGRAM USES CATDSPZ: checked" in got("what_uses", name="CATDSPZ",
                                                          kind="screens")
         and "NOT PROVEN" not in got("what_uses", name="CATDSPZ", kind="screens")),
        # ---- T4: a field with no record never matches every group move -------
        ("T4", "a copybook 01 with no record returns only its own users",
         lambda: "LOADMAIN" not in got("where_is_field_used", field="LNK-REQ")
         and "ORDUPD" not in got("where_is_field_used", field="LNK-REQ")),
        ("T4", "SQL table and column names are never read as COBOL field uses",
         lambda: not rows("SELECT field FROM field_usage WHERE program IN "
                          "('LOADMAIN','LOADOTH','ORDUPD')")),
        # ---- T8: an exclusion is a finding and carries evidence ---------------
        ("T8", "an exclusion with no member:line or tool call does not place a member",
         lambda: _unsupported("\n".join([
             "| LOADCL | excluded - other platform |",
             "| SWAPCAT | excluded: who_calls(SWAPCAT) is empty |",
             "| GETCAT | not affected, reads only (GETCAT:5) |",
             "| LOADMAIN | recompile |",
             "| ORDUPD | excluded - other platform |",
             "| ORDUPD | recompile |"])) == {"LOADCL"}),
        # ---- T5: a scoped search names the types it did not read -------------
        ("T5", "a search scoped to one type lists the code types it skipped",
         lambda: "NOT SEARCHED:" in got("search_source", pattern="LNK-TEXT", member_type="CBLLE")
         and "CPY (" in got("search_source", pattern="LNK-TEXT", member_type="CBLLE")),
        ("T5", "member_type CODE searches every code type, copybooks included",
         lambda: "LNKREQ:" in got("search_source", pattern="LNK-TEXT", member_type="CODE")
         and "code types" not in got("search_source", pattern="LNK-TEXT", member_type="CODE")),
        # ---- P7/S12: coverage says which libraries yielded nothing --------------
        ("P7", "a library whose members yield no facts is INDEXED BUT EMPTY",
         lambda: any("EMPTYLIB" in line and "INDEXED BUT EMPTY" in line
                     for line in got("coverage_report").splitlines())),
        ("S12", "coverage gives the build date and facts per library",
         lambda: "index built: " in got("coverage_report")
         and "with facts" in got("coverage_report")),
        # ---- T9: the unresolved lines themselves ----------------------------
        ("T9", "what_it_touches kind=unresolved shows the source line and why",
         lambda: "device_link_not_extracted" in got("what_it_touches", name="SCRPGME",
                                                    kind="unresolved")
         and "WORKSTATION-CATDSPF" in got("what_it_touches", name="SCRPGME",
                                          kind="unresolved")),
        # ---- P5/T11: byte positions land on fields -----------------------------
        ("P5", "a COBOL offset into a copied DDS record maps to the field there",
         lambda: set(rows("SELECT direction, field, field_start, field_length "
                          "FROM positional_access WHERE member='POSPGM'"))
         == {("read", "POSFLG", 16, 1), ("write", "POSFLG", 16, 1)}),
        ("P5", "an offset into a LINKAGE record maps to the copybook field",
         lambda: rows("SELECT field, field_start FROM positional_access "
                      "WHERE member='POSLNK'") == [("LNK-AMT", 11)]),
        ("P5", "a CL %SST on a variable passed to a program maps into its LINKAGE",
         lambda: rows("SELECT field, direction, via FROM positional_access "
                      "WHERE member='POSCL' AND variable='OKREQ'")
         == [("LNK-FLAG", "write", "passed as parameter 1 to RECVREQ")]),
        ("P5", "an offset no layout places is recorded as unresolved, not dropped",
         lambda: rows("SELECT field FROM positional_access WHERE member='POSCL' "
                      "AND variable='LOOSE'") == [("",)]
         and rows("SELECT COUNT(*) FROM unresolved WHERE member='POSCL' "
                  "AND kind='position_not_mapped'") == [(1,)]),
        ("T11", "where_is_field_used lists the sites that reach a field by position",
         lambda: "REACHED BY BYTE POSITION (2)" in got("where_is_field_used", field="POSFLG")
         and "POSCL:" in got("where_is_field_used", field="LNK-FLAG")),
        ("T11", "describe_file gives each field's byte range",
         lambda: all(f in got("describe_file", file="ORDPOS")
                     for f in ("1-10", "11-15", "16-16", "17-36"))),
        # ---- P9/S9: header "inactive" is a hint weighed against reachability ---
        ("P9", "a retired header is recorded as a hint with its line",
         lambda: rows("SELECT member, line FROM header_hint WHERE kind='inactive' "
                      "ORDER BY member") == [("OLDLIVE", 2), ("OLDPGM", 2)]),
        ("S9", "a retired member nothing reaches is at most Possible",
         lambda: "NOTHING IN THE SOURCE REACHES IT" in got("find_object", name="OLDPGM")
         and "At most Possible" in got("find_object", name="OLDPGM")),
        ("S9", "a retired member something still calls is reported reachable",
         lambda: "STILL REACHABLE" in got("who_calls", name="OLDLIVE")
         and "OLDCL" in got("who_calls", name="OLDLIVE")),
        # ---- S8: callers followed until a root -----------------------------------
        ("S8", "a caller with callers above the depth is not a root yet",
         lambda: "NOT A ROOT YET" in got("who_calls", name="OLDLIVE")
         and "depth=2" in got("who_calls", name="OLDLIVE")),
        ("S8", "at enough depth the chain ends in a named root",
         lambda: "ROOTS - nothing in the source calls these" in got(
             "who_calls", name="OLDLIVE", depth=2)
         and "TOPCL" in got("who_calls", name="OLDLIVE", depth=2)
         and "NOT A ROOT YET" not in got("who_calls", name="OLDLIVE", depth=2)),
        # ---- T10: identifiers before the ranking --------------------------------
        ("T10", "an identifier the index has never seen is stated before the ranking",
         lambda: got("trace_ticket", text="POSFLG is wrong for QZPX orders")
         .startswith("NOT IN THE INDEX: QZPX")
         and "POSFLG" not in got("trace_ticket", text="POSFLG is wrong for QZPX orders")
         .split("\n")[0]),
        ("T10", "a field the ticket names brings in the file that declares it",
         lambda: "declares POSFLG, named in the ticket"
         in got("trace_ticket", text="POSFLG is wrong for QZPX orders")),
        # ---- P8: copybook extensions stay indexed -------------------------------
        ("P8", "a .CBLLECPY member is indexed as a copybook",
         lambda: rows("SELECT member_type FROM object WHERE name='EXTCPY'") == [("CPY",)]),
        ("P8", "a program's COPY of it resolves to the copybook",
         lambda: rows("SELECT kind FROM copy_edge WHERE program='EXTUSE' "
                      "AND copybook='EXTCPY'") == [("copybook",)]
         and not rows("SELECT 1 FROM unresolved WHERE member='EXTUSE' "
                      "AND raw LIKE '%EXTCPY%'")),
        ("EQ1", "no spelling of a figurative constant is taken for a field",
         lambda: not rows(
             "SELECT DISTINCT field FROM field_usage WHERE program='FIGCON' "
             "AND field IN ('HIGH-VALUE','HIGH-VALUES','LOW-VALUE',"
             "'LOW-VALUES','SPACE','SPACES','ZERO','ZEROS','ZEROES',"
             "'NULL','NULLS','QUOTE','QUOTES','ALL')")),
        ("EQ2", "the singular forms still record their target as written",
         lambda: {r[0] for r in rows(
             "SELECT field FROM field_usage WHERE program='FIGCON' "
             "AND direction='write'")}
         >= {"FG-KEY", "FG-END", "FG-NAME", "FG-NUM"}),
        ("EQ3", "MOVE ALL <constant> records its target, like any other MOVE",
         lambda: {r[0] for r in rows(
             "SELECT field FROM field_usage WHERE program='FIGCON' "
             "AND direction='write'")}
         >= {"FG-COUNT", "FG-ADDR"}),
        ("UC1", "an unresolvable CALL is stored, not dropped",
         lambda: rows("SELECT confidence, variable FROM call_edge "
                      "WHERE caller='RUNCALL' AND (callee IS NULL "
                      "OR callee='')") == [("unresolved", "RC-PGM")]),
        ("UC2", "blast_radius says a call could not be followed",
         lambda: "NOT FOLLOWED" in got("blast_radius", name="RUNCALL")
         and "RC-PGM" in got("blast_radius", name="RUNCALL")),
        ("UC3", "blast_radius cites the line, so the report can quote it",
         lambda: "RUNCALL:" in got("blast_radius", name="RUNCALL")),
        ("UC4", "what_it_calls shows the variable, never a blank cell",
         lambda: "(through RC-PGM)" in got("what_it_calls", name="RUNCALL")),
        ("UC5", "the resolved call beside it is unaffected",
         lambda: "FIGCON" in got("what_it_calls", name="RUNCALL")),
        ("T7", "program_interface on the CL lists the calls it makes",
         lambda: "RECVREQ at SENDCL:" in got("program_interface", program="SENDCL")
         and "&REQ=200" in got("program_interface", program="SENDCL")),
    ]


def _unsupported(text: str) -> set:
    from kb.server import accounting
    # Member names only: this runs without an index to validate tokens against.
    names = {line.split("|")[1].strip() for line in text.splitlines()}
    return set(accounting.unsupported_exclusions(text, names))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", help="build in this folder and keep it")
    args = parser.parse_args(argv)
    work = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="coupling-"))
    tree, data = work / "tree", work / "kb"
    shutil.rmtree(tree, ignore_errors=True)
    shutil.rmtree(data, ignore_errors=True)
    try:
        build_tree(tree)
        index(tree, data)
        results = []
        for layer, name, test in checks(tree, data):
            try:
                ok = bool(test())
            except Exception as exc:                          # noqa: BLE001
                ok = False
                name += f"  ({type(exc).__name__}: {exc})"
            results.append((layer, name, ok))
            print(f"{'PASS' if ok else 'FAIL'}  {layer:<3} {name}")
        failed = [r for r in results if not r[2]]
        print(f"\n{len(results) - len(failed)}/{len(results)} passed")
        return 1 if failed else 0
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
