"""
Ticket-reader regression fixture: every shape a Jira export arrives in.

    python -m kb.eval.ticket_fixture              build, parse, check
    python -m kb.eval.ticket_fixture --keep DIR   keep the files

Checks the reader against files it builds itself - a Jira XML export, a Word
export, an HTML export, plain text, an empty file, a PDF with no text - and,
when a browser is installed, a real printed PDF, which is what a ticket
actually arrives as. Every name in here is invented.

Dev-only: kb.eval is excluded from the build.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]
KEY = "CATLIB-4711"

BROWSERS = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe")

TICKET_HTML = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<title>[{key}] Zusatzpaket fehlt in der Ausgabedatei - Jira</title></head><body>
<h1>[{key}] Zusatzpaket fehlt in der Ausgabedatei</h1>
<table>
<tr><td>Typ:</td><td>Fehler</td><td>Status:</td><td>In Arbeit</td></tr>
<tr><td>Priorit&auml;t:</td><td>Hoch</td><td>L&ouml;sung:</td><td>Nicht erledigt</td></tr>
<tr><td>Melder:</td><td>A. Beispiel</td><td>Bearbeiter:</td><td>S. Muster</td></tr>
</table>
<h2>Beschreibung</h2>
<p>Die Position wird im Ausdruck gezeigt, fehlt aber in der Ausgabedatei.
Der Betrag erscheint nicht in der Summenzeile. Betrifft PGM410 und TAB_ZUS.</p>
<h2>Akzeptanzkriterien</h2>
<ol>
<li>AC-001: Die Position erscheint in der Ausgabedatei.</li>
<li>AC-002: Der Betrag wird in der Summenzeile ber&uuml;cksichtigt.</li>
<li>AC-007: Die Position wird im Auftrag gekennzeichnet (siehe OQ-7).</li>
</ol>
<h2>Kommentare</h2>
<p>S. Muster hat einen Kommentar hinzugef&uuml;gt - 03.09.2026 11:20<br>
Ursache: der Aufruf &uuml;bergibt das Kennzeichen nicht.</p>
<p>A. Beispiel hat einen Kommentar hinzugef&uuml;gt - 04.09.2026 08:05<br>
Bitte auch den Fall Gutschrift pr&uuml;fen.</p>
</body></html>""".format(key=KEY)

JIRA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="0.92"><channel><item>
  <title>[{key}] Zusatzpaket fehlt in der Ausgabedatei</title>
  <key id="45011">{key}</key>
  <summary>Zusatzpaket fehlt in der Ausgabedatei</summary>
  <type id="1">Fehler</type>
  <priority id="2">Hoch</priority>
  <status id="3">In Arbeit</status>
  <resolution id="-1">Nicht erledigt</resolution>
  <assignee username="smuster">S. Muster</assignee>
  <reporter username="abeispiel">A. Beispiel</reporter>
  <created>Wed, 2 Sep 2026 09:14:00 +0200</created>
  <updated>Fri, 4 Sep 2026 16:02:00 +0200</updated>
  <component>Fakturierung</component>
  <labels><label>xrechnung</label><label>zugferd</label></labels>
  <description>&lt;p&gt;Die Position wird im Ausdruck gezeigt, fehlt aber in der
  Ausgabedatei. Betrifft &lt;b&gt;PGM410&lt;/b&gt; und TAB_ZUS.&lt;/p&gt;</description>
  <comments>
    <comment id="1" author="smuster" created="Thu, 3 Sep 2026 11:20:00 +0200">
      &lt;p&gt;Ursache: der Aufruf uebergibt das Kennzeichen nicht.&lt;/p&gt;</comment>
    <comment id="2" author="abeispiel" created="Fri, 4 Sep 2026 08:05:00 +0200">
      &lt;p&gt;Bitte auch den Fall Gutschrift pruefen.&lt;/p&gt;</comment>
  </comments>
  <attachments><attachment id="9" name="beispiel.xml" size="812"/></attachments>
  <issuelinks><issuelinktype id="10"><outwardlinks description="relates to">
    <issuelink><issuekey id="44900">CATLIB-4600</issuekey></issuelink>
  </outwardlinks></issuelinktype></issuelinks>
  <customfields>
    <customfield id="customfield_101" key="com.example:textarea">
      <customfieldname>Akzeptanzkriterien</customfieldname>
      <customfieldvalues>&lt;p&gt;AC-001: Die Position erscheint in der Ausgabedatei.&lt;br/&gt;
      AC-002: Der Betrag wird in der Summenzeile beruecksichtigt.&lt;br/&gt;
      AC-007: Die Position wird im Auftrag gekennzeichnet.&lt;/p&gt;</customfieldvalues>
    </customfield>
  </customfields>
</item></channel></rss>""".format(key=KEY)

TEXT_TICKET = """[{key}] Zusatzpaket fehlt in der Ausgabedatei

Typ: Fehler
Status: In Arbeit
Melder: A. Beispiel

Beschreibung
Die Position wird im Ausdruck gezeigt, fehlt aber in der Ausgabedatei.

Akzeptanzkriterien
AC-001: Die Position erscheint in der Ausgabedatei.
AC-002: Der Betrag wird in der Summenzeile beruecksichtigt.

Kommentare
Comment by S. Muster [ 03/Sep/26 11:20 ]
Ursache: der Aufruf uebergibt das Kennzeichen nicht.
""".format(key=KEY)

#: A one-page PDF that draws a rectangle and no text at all - a scan, as far
#: as any reader can tell. Built by hand so the fixture needs no tools.
def _image_only_pdf() -> bytes:
    content = b"0.9 g 100 500 400 200 re f\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content
        + b"endstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += ("%010d 00000 n \n" % offset).encode()
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n")
    return bytes(out)


def _docx(text: str) -> bytes:
    import io
    paragraphs = "".join(
        "<w:p><w:r><w:t xml:space=\"preserve\">%s</w:t></w:r></w:p>"
        % line.replace("&", "&amp;").replace("<", "&lt;")
        for line in text.split("\n"))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body>' + paragraphs
                + "</w:body></w:document>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml",
                         '<?xml version="1.0"?><Types xmlns="http://schemas.'
                         'openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '</Types>')
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def build(root: Path) -> Tuple[Path, Optional[Path]]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "ticket.html").write_text(TICKET_HTML, encoding="utf-8")
    (root / (KEY + ".xml")).write_text(JIRA_XML, encoding="utf-8")
    (root / (KEY + ".txt")).write_text(TEXT_TICKET, encoding="utf-8")
    (root / (KEY + ".docx")).write_bytes(_docx(TEXT_TICKET))
    (root / "scan.pdf").write_bytes(_image_only_pdf())
    # A readable ticket that simply has no acceptance criteria.
    (root / "plain.txt").write_text(
        "\n".join(["[CATLIB-4090] Sortierung im Auszug", "",
                   "Beschreibung",
                   "Die Zeilen stehen in der falschen Reihenfolge.", ""]),
        encoding="utf-8")
    (root / "empty.pdf").write_bytes(b"")
    # Jira's older Word export: HTML with a .doc name.
    (root / (KEY + ".doc")).write_text(TICKET_HTML, encoding="utf-8")

    printed: Optional[Path] = None
    for browser in BROWSERS:
        if not Path(browser).exists():
            continue
        target = root / (KEY + ".pdf")
        try:
            subprocess.run([browser, "--headless", "--disable-gpu",
                            "--no-pdf-header-footer",
                            "--print-to-pdf=" + str(target),
                            (root / "ticket.html").as_uri()],
                           capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError):
            continue
        if target.exists() and target.stat().st_size > 1000:
            printed = target
            break
    return root, printed


Check = Tuple[str, str, Callable[[], bool]]


def checks(root: Path, printed: Optional[Path]) -> List[Check]:
    from kb import ticket as reader

    parsed = {name: reader.parse(root / name) for name in (
        KEY + ".xml", KEY + ".txt", KEY + ".docx", KEY + ".doc",
        "scan.pdf", "empty.pdf", "plain.txt")}
    if printed is not None:
        parsed["printed"] = reader.parse(printed)

    def t(name):
        return parsed[name].ticket

    out: List[Check] = [
        # ---- Jira XML: the structured path ---------------------------------
        ("XML", "the key, title and fields come from the export",
         lambda: t(KEY + ".xml").key == KEY
         and "Ausgabedatei" in t(KEY + ".xml").title
         and t(KEY + ".xml").fields.get("status") == "In Arbeit"
         and t(KEY + ".xml").fields.get("assignee") == "S. Muster"),
        ("XML", "every comment is kept, in order, with author and date",
         lambda: [c.author for c in t(KEY + ".xml").comments] == ["smuster", "abeispiel"]
         and "Kennzeichen" in t(KEY + ".xml").comments[0].body),
        ("XML", "acceptance criteria come out of the custom field",
         lambda: [c.id for c in t(KEY + ".xml").criteria] == ["AC-001", "AC-002", "AC-007"]),
        ("XML", "attachments and linked tickets are listed",
         lambda: t(KEY + ".xml").attachments == ["beispiel.xml"]
         and "CATLIB-4600" in t(KEY + ".xml").links),
        ("XML", "HTML in the description is flattened to text",
         lambda: "<p>" not in t(KEY + ".xml").description
         and "PGM410" in t(KEY + ".xml").description),
        ("XML", "an entity declaration is refused, not expanded",
         lambda: _entity_refused(root)),

        # ---- printed and exported shapes -----------------------------------
        ("TEXT", "a plain-text ticket yields fields, criteria and a comment",
         lambda: t(KEY + ".txt").key == KEY
         and [c.id for c in t(KEY + ".txt").criteria] == ["AC-001", "AC-002"]
         and t(KEY + ".txt").comments[0].author == "S. Muster"),
        ("DOCX", "a Word export is read",
         lambda: t(KEY + ".docx").key == KEY
         and len(t(KEY + ".docx").criteria) == 2),
        ("HTML", "Jira's older Word export (HTML named .doc) is read",
         lambda: t(KEY + ".doc").key == KEY
         and len(t(KEY + ".doc").comments) == 2
         and t(KEY + ".doc").fields.get("status") == "In Arbeit"),

        # ---- failures are findings, not silence ----------------------------
        ("GAP", "a page with no text is reported as one to read by eye",
         lambda: t("scan.pdf").unreadable_pages == [1]
         and any("image" in n for n in t("scan.pdf").notes)),
        ("GAP", "an empty file is refused with a plain reason",
         lambda: any("empty" in n for n in t("empty.pdf").notes)
         and not t("empty.pdf").criteria),
        ("GAP", "a readable ticket with no criteria says so, rather than staying silent",
         lambda: any("no acceptance criteria" in n
                     for n in t("plain.txt").notes)),
        ("GAP", "a file that is all images is not reported as a ticket with no criteria",
         lambda: not any("no acceptance criteria" in n
                         for n in t("scan.pdf").notes)),

        # ---- the XML the analysis reads ------------------------------------
        ("OUT", "the rendered XML carries criteria, comments and quality",
         lambda: all(mark in parsed[KEY + ".xml"].xml for mark in (
             '<acceptance-criteria count="3"', '<criterion id="AC-007"',
             '<comments count="2"', "<quality>", "<identifiers>"))),
        ("OUT", "the raw page text is kept so nothing read is lost",
         lambda: "<pages count=" in parsed[KEY + ".txt"].xml
         and "Ausdruck" in parsed[KEY + ".txt"].xml),
        ("OUT", "the XML is well formed",
         lambda: _parses(parsed[KEY + ".xml"].xml)
         and _parses(parsed[KEY + ".txt"].xml)
         and _parses(parsed["scan.pdf"].xml)),
        ("OUT", "identifiers worth a tool call are listed, ticket keys are not",
         lambda: "PGM410" in t(KEY + ".xml").identifiers
         and KEY not in t(KEY + ".xml").identifiers
         and not any(i.startswith("AC-") for i in t(KEY + ".xml").identifiers)),

        # ---- the hook ------------------------------------------------------
        ("HOOK", "a message naming a ticket file gets the ticket as context",
         lambda: _hook_context(root, "please analyse @%s.xml now" % KEY)),
        ("HOOK", "a message naming a ticket key finds the export beside it",
         lambda: _hook_context(root, "start work on %s" % KEY)),
        ("HOOK", "a message naming no ticket adds nothing",
         lambda: _hook_quiet(root, "what does PGM410 do?")),
        ("HOOK", "a path that does not exist never fails the turn",
         lambda: _hook_quiet(root, "read @Tickets/nothing-here.pdf")),
    ]

    if printed is not None:
        out += [
            ("PDF", "a browser-printed ticket is read, umlauts included",
             lambda: t("printed").key == KEY
             and "Priorität" in "\n".join(t("printed").pages)),
            ("PDF", "its acceptance criteria keep their own ids",
             lambda: [c.id for c in t("printed").criteria]
             == ["AC-001", "AC-002", "AC-007"]),
            ("PDF", "its comments are split by author and date",
             lambda: len(t("printed").comments) == 2
             and t("printed").comments[0].author == "S. Muster"),
            ("PDF", "fields on one printed line are all read",
             lambda: t("printed").fields.get("status") == "In Arbeit"
             and t("printed").fields.get("resolution") == "Nicht erledigt"),
        ]
    out += [
        ("DROP", "one dropped ticket is returned once, not once per spelling",
         _drop_once),
        ("DROP", "a drop folder inside the configured source tree is found",
         _drop_in_source_tree),
        ("DROP", "the same folder reached from two roots is read once",
         _drop_overlap),
        ("DROP", "a broken configuration never stops the hook",
         _drop_config_broken),
        ("DROP", "no installation's folder name is written into the code",
         _drop_names_generic),
    ]
    return out


def _drop_tree(names):
    base = Path(tempfile.mkdtemp(prefix="drop-"))
    for rel in names:
        f = base / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("<rss/>", encoding="utf-8")
    return base


def _with_source(root):
    """Point the configured source root somewhere for one check."""
    from kb import config
    class _Swap:
        def __enter__(self):
            self.old = config.SOURCE_ROOT
            config.SOURCE_ROOT = root
        def __exit__(self, *exc):
            config.SOURCE_ROOT = self.old
    return _Swap()


def _drop_once() -> bool:
    from kb.ticket import hook
    base = _drop_tree(["work/tickets/ALPHAPLUS-1.xml"])
    try:
        with _with_source(base / "nowhere"):
            got = hook.dropped(base / "work")
        return [p.name for p in got] == ["ALPHAPLUS-1.xml"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _drop_in_source_tree() -> bool:
    from kb.ticket import hook
    base = _drop_tree(["checkout/SRCROOT/tickets/ALPHAPLUS-2.xml"])
    try:
        (base / "work").mkdir()
        with _with_source(base / "checkout" / "SRCROOT"):
            got = hook.dropped(base / "work")
        return [p.name for p in got] == ["ALPHAPLUS-2.xml"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _drop_overlap() -> bool:
    from kb.ticket import hook
    base = _drop_tree(["tree/tickets/ALPHAPLUS-3.xml"])
    try:
        with _with_source(base / "tree"):
            got = hook.dropped(base / "tree")      # cwd IS the source root
        return [p.name for p in got] == ["ALPHAPLUS-3.xml"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _drop_config_broken() -> bool:
    from kb.ticket import hook
    base = _drop_tree(["work/tickets/ALPHAPLUS-4.xml"])
    try:
        with _with_source(object()):               # not a path at all
            got = hook.dropped(base / "work")
        return [p.name for p in got] == ["ALPHAPLUS-4.xml"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _drop_names_generic() -> bool:
    from kb.ticket import hook
    return all("/" not in n and "\\" not in n for n in hook._DROP_NAMES)


def _parses(xml: str) -> bool:
    from xml.etree import ElementTree
    try:
        ElementTree.fromstring(xml)
        return True
    except Exception:                                     # noqa: BLE001
        return False


def _entity_refused(root: Path) -> bool:
    from kb import ticket as reader
    hostile = root / "hostile.xml"
    hostile.write_text(
        '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "boom">]>'
        '<rss><channel><item><key>X-1</key><summary>&x;</summary>'
        "</item></channel></rss>", encoding="utf-8")
    parsed = reader.parse(hostile)
    return any("DOCTYPE" in note for note in parsed.ticket.notes)


def _hook_output(root: Path, prompt: str) -> str:
    import io
    import json
    from contextlib import redirect_stdout
    from kb.ticket import hook

    payload = json.dumps({"prompt": prompt, "cwd": str(root)})
    buffer = io.StringIO()
    stdin = sys.stdin
    sys.stdin = io.StringIO(payload)
    try:
        with redirect_stdout(buffer):
            code = hook.main()
    finally:
        sys.stdin = stdin
    assert code == 0, "the hook must always exit 0"
    return buffer.getvalue()


def _hook_context(root: Path, prompt: str) -> bool:
    import json
    raw = _hook_output(root, prompt)
    if not raw.strip():
        return False
    payload = json.loads(raw)
    context = payload["hookSpecificOutput"]["additionalContext"]
    return "<acceptance-criteria" in context and KEY in context


def _hook_quiet(root: Path, prompt: str) -> bool:
    return _hook_output(root, prompt).strip() == ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", help="build in this folder and keep it")
    args = parser.parse_args(argv)
    work = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="ticket-"))
    try:
        root, printed = build(work / "tickets")
        if printed is None:
            print("  (no browser found - the printed-PDF checks are skipped)")
        results = []
        for layer, name, test in checks(root, printed):
            try:
                ok = bool(test())
            except Exception as exc:                      # noqa: BLE001
                ok = False
                name += f"  ({type(exc).__name__}: {exc})"
            results.append(ok)
            print(f"{'PASS' if ok else 'FAIL'}  {layer:<5} {name}")
        failed = results.count(False)
        print(f"\n{len(results) - failed}/{len(results)} passed")
        return 1 if failed else 0
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
