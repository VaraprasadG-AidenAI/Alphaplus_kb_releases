"""
Write the pyramid: menus, then families, then libraries.

    python -m kb.summarise.pyramid --estimate
    python -m kb.summarise.pyramid --level menu --limit 3
    python -m kb.summarise.pyramid
    python -m kb.summarise.pyramid --status

Children before parents, because a family page is written from its menus'
pages and a library page from its families'.

Serial on purpose. Inference here is compute-bound on a CPU, so running two
calls at once splits the same cores and finishes no sooner - concurrency is a
throughput fix for latency-bound work, which this is not.

Resumable from the `summary` table: a page that is present and fresh is
skipped, and freshness understands roll-ups (see store.is_fresh). A run that
dies at menu 60 of 92 picks up at 61.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config
from . import prompts, sheets, store
from .provider import Provider, ProviderError

#: Levels in the order they must be written.
ORDER = ("menu", "family", "library")


def free_memory_gb() -> Optional[float]:
    """Physical memory available, or None if we cannot tell.

    Standard library only, like everything else here.
    """
    try:
        if sys.platform == "win32":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong),
                            ("load", ctypes.c_ulong),
                            ("total_phys", ctypes.c_ulonglong),
                            ("avail_phys", ctypes.c_ulonglong),
                            ("total_page", ctypes.c_ulonglong),
                            ("avail_page", ctypes.c_ulonglong),
                            ("total_virt", ctypes.c_ulonglong),
                            ("avail_virt", ctypes.c_ulonglong),
                            ("avail_ext", ctypes.c_ulonglong)]

            status = Status()
            status.length = ctypes.sizeof(Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(
                    ctypes.byref(status)):
                return None
            return status.avail_phys / (1024 ** 3)
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 ** 2)
    except Exception:
        return None
    return None


#: Refuse to start below this much free memory, in GB.
#:
#: A local model is held in RAM for the whole run. When it does not fit, nothing
#: fails - the machine pages the weights in and out and a call that took 90
#: seconds takes twenty minutes, at 100% CPU, looking exactly like work. That
#: happened here: an 18.7 GB model on a 31.5 GB machine with a browser and an
#: editor open left 1.6 GB free and 434 pages/sec of swapping, and the run sat
#: on one menu for nineteen minutes before anyone looked.
#:
#: Six is not a model size, it is headroom. The check cannot know how big the
#: model is - Ollama loads it - so it checks that there is room to breathe.
MIN_FREE_GB = 6.0


def _units(db: sqlite3.Connection, scope: str) -> List[Tuple[str, str]]:
    """(key, library) for everything at one level."""
    db.row_factory = sqlite3.Row
    if scope == "menu":
        return [(r["menu"], r["library"]) for r in db.execute(
            "SELECT DISTINCT menu, library FROM menu_option ORDER BY menu")]
    if scope == "family":
        seen: Dict[str, str] = {}
        for r in db.execute("SELECT DISTINCT menu, library FROM menu_option "
                            "ORDER BY menu"):
            seen.setdefault(sheets.family_of(r["menu"]), r["library"])
        return sorted(seen.items())
    if scope == "library":
        return [(r["library"], r["library"]) for r in db.execute(
            "SELECT DISTINCT library FROM object WHERE library<>'' "
            "ORDER BY library")]
    raise ValueError(scope)


def _sheet_for(db: sqlite3.Connection, scope: str,
               key: str) -> Tuple[str, str, str]:
    """(sheet, source, child_hash) for one unit."""
    if scope == "menu":
        sheet, source = sheets.menu_sheet(db, key)
        return sheet, source, ""
    if scope == "family":
        menus = sheets.family_menus(db, key)
        return (sheets.family_sheet(db, key), "",
                store.children_hash(db, "menu", menus))
    families = sorted({sheets.family_of(r[0]) for r in db.execute(
        "SELECT DISTINCT menu FROM menu_option WHERE library=?", (key,))})
    return (sheets.library_sheet(db, key), "",
            store.children_hash(db, "family", families))


def estimate(db: sqlite3.Connection) -> None:
    """What the run will cost, from measured throughput rather than a guess."""
    # Measured on this machine with qwen3-coder:30b: 23.9 tok/s prefill,
    # 10 tok/s decode. Prefill dominates, which is why the sheets are budgeted
    # by input size rather than output length.
    prefill, decode = 23.9, 10.0
    print()
    print(f"  {'level':<10}{'units':>7}{'prompt tok':>12}{'out tok':>9}"
          f"{'estimate':>12}")
    print("  " + "-" * 52)
    grand = 0.0
    for scope in ORDER:
        units = _units(db, scope)
        if not units:
            continue
        sample = units[:8]
        tokens = 0
        for key, _library in sample:
            sheet, source, _ = _sheet_for(db, scope, key)
            tokens += len(prompts.level_prompt(scope, sheet, source)) // 4
        per_prompt = tokens / max(1, len(sample))
        _t, _cap, budget = prompts.TEMPLATES[scope]
        seconds = len(units) * (per_prompt / prefill + budget * 0.6 / decode)
        grand += seconds
        print(f"  {scope:<10}{len(units):>7}{per_prompt:>12,.0f}"
              f"{budget:>9}{seconds / 60:>10.0f}m")
    print("  " + "-" * 52)
    print(f"  {'TOTAL':<10}{'':>7}{'':>12}{'':>9}{grand / 3600:>10.1f}h")
    print()
    print("  Nothing leaves this machine: the provider is a local server.")
    print()


def build(level: Optional[str] = None, limit: Optional[int] = None,
          refresh: bool = False, dry_run: bool = False) -> int:
    if not config.ENABLE_SUMMARIES:
        print("summaries are switched off. To turn them on:\n"
              "    set ALPHAPLUS_SUMMARIES=1\n"
              "    set ALPHAPLUS_LLM=local\n"
              "    set ALPHAPLUS_LLM_MODEL=qwen3-coder:30b\n"
              "    set ALPHAPLUS_LLM_SOURCE=1", file=sys.stderr)
        return 1
    from kb import store as kbstore
    ok, why = kbstore.ready()
    if not ok:
        print(why, file=sys.stderr)
        return 1

    db = kbstore.facts(read_only=False)
    provider = Provider()
    usable = provider.usable
    if not usable:
        print(f"  no model ({provider.why_not()})")
        print("  writing fact-only pages - nothing leaves this machine, and "
              "they will be\n  rewritten when a model is configured.")

    free = free_memory_gb()
    if usable and free is not None:
        print(f"  {free:.1f} GB of memory free")
        if free < MIN_FREE_GB:
            print(f"\n  Not enough memory free to hold a local model without "
                  f"paging.\n"
                  f"  Below about {MIN_FREE_GB:.0f} GB the weights get swapped "
                  f"in and out and a page\n"
                  f"  that takes ninety seconds takes twenty minutes, at full "
                  f"CPU, looking\n"
                  f"  exactly like progress. Close what you can and start "
                  f"again, or use a\n"
                  f"  smaller model. Overriding this will not make it faster.",
                  file=sys.stderr)
            return 1

    started = time.time()
    written = skipped = failed = 0
    slowest = 0.0

    for scope in ORDER:
        if level and scope != level:
            continue
        units = _units(db, scope)
        if limit:
            units = units[:limit]
        if not units:
            continue
        print(f"\n  {scope} ({len(units)})")

        for index, (key, library) in enumerate(units, 1):
            sheet, source, child_hash = _sheet_for(db, scope, key)
            prompt = prompts.level_prompt(scope, sheet, source)
            input_hash = store.digest(prompt)
            existing = store.read(db, scope, key, library)

            if not refresh and store.is_fresh(existing, input_hash,
                                              child_hash, usable):
                skipped += 1
                continue
            if dry_run:
                print(f"    {index:>4}/{len(units)}  {key:<12}"
                      f"{len(prompt) // 4:>6} tok")
                written += 1
                continue

            if not usable:
                store.write(db, store.Page(
                    scope=scope, key=key, library=library,
                    text="_No description - no model is configured. The facts "
                         "below are read from source._",
                    written_by=store.FACTS_ONLY, input_hash=input_hash,
                    child_hash=child_hash), sheet)
                written += 1
                continue

            _template, cap, budget = prompts.TEMPLATES[scope]
            call_started = time.time()
            try:
                text = provider.complete(prompts.SYSTEM, prompt,
                                         max_tokens=budget)
            except ProviderError as exc:
                failed += 1
                print(f"    ! {key:<12}{str(exc)[:70]}")
                # The same circuit breaker the member tier uses: if nothing at
                # all is succeeding, stop rather than spend an hour proving it.
                if failed > 20 and written == 0:
                    print("    stopping - nothing is succeeding")
                    db.commit()
                    return 1
                continue

            store.write(db, store.Page(
                scope=scope, key=key, library=library, text=text.strip(),
                written_by=f"{provider.name}/{provider.model}",
                input_hash=input_hash, child_hash=child_hash,
                tokens_in=len(prompt) // 4,
                tokens_out=len(text) // 4), sheet)
            written += 1

            # Committed per page, not per batch. A page costs about seventy
            # seconds of CPU inference and a commit costs microseconds, so
            # batching saves nothing measurable and risks losing an hour's work
            # to one crash. It also makes progress observable from outside the
            # process, which matters when the run takes two hours.
            db.commit()

            took = time.time() - call_started
            elapsed = time.time() - started
            rate = elapsed / written
            left = (len(units) - index) * rate
            print(f"    {index:>4}/{len(units)}  {key:<12}"
                  f"{len(text.split()):>4} words  {took:>4.0f}s  "
                  f"~{left / 60:>4.0f}m left", flush=True)

            # A slow page on its own means nothing - legitimate variance here
            # runs from 13s to 150s depending on prompt length and whether the
            # weights are still warm, and a warning that fires on that is
            # noise that teaches you to ignore warnings. Thrashing is a slow
            # page AND no memory to hold the model, so both are required.
            if (slowest and took > 4 * slowest and written > 3
                    and (free_memory_gb() or 99) < MIN_FREE_GB):
                print(f"    ! that page took {took:.0f}s against a "
                      f"{slowest:.0f}s best, with under {MIN_FREE_GB:.0f} GB "
                      f"free.\n      The model is being paged in and out. "
                      f"Stopping to free memory will\n      finish sooner "
                      f"than letting it run.", flush=True)
            slowest = took if not slowest else min(slowest, took)

    db.commit()
    took = time.time() - started
    print(f"\n  written {written}, skipped {skipped}, failed {failed} "
          f"in {took / 60:.0f}m")
    if usable:
        print(f"  {provider.cost_line()}")
    db.close()
    return 0


def import_pages() -> int:
    """Rebuild the `summary` table from the pages already on disk.

    Written because a full index rebuild used to drop the table and 112 pages'
    worth of model output went with it. `build.py` now carries those rows
    forward, so this is the repair for a database that already lost them - and
    the way to adopt pages generated on another machine.

    The prose is taken from the page, and `input_hash` is recomputed from the
    facts as they are now. That is the honest reading: if the facts have moved
    since the page was written, the page IS stale, and recomputing rather than
    inventing a hash means `--stale` will say so.
    """
    from kb import store as kbstore
    ok, why = kbstore.ready()
    if not ok:
        print(why, file=sys.stderr)
        return 1

    db = kbstore.facts(read_only=False)
    found = imported = skipped = 0

    for scope in ORDER:
        for key, library in _units(db, scope):
            path = store.page_path(scope, key)
            if not path.exists():
                continue
            found += 1
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue

            prose = _prose_of(text)
            if not prose:
                skipped += 1
                continue

            sheet, source, child_hash = _sheet_for(db, scope, key)
            prompt = prompts.level_prompt(scope, sheet, source)
            store.write(db, store.Page(
                scope=scope, key=key, library=library, text=prose,
                written_by=_written_by_of(text) or "imported",
                input_hash=store.digest(prompt), child_hash=child_hash), sheet)
            imported += 1
        db.commit()

    db.commit()
    db.close()
    print(f"  {found} page(s) on disk, {imported} imported, "
          f"{skipped} without readable prose")
    if imported:
        print("  re-embed them with: python -m kb semantic")
    return 0


def _prose_of(page: str) -> str:
    """The generated prose out of a written page.

    A page is `# KEY`, the prose, then a `---` rule and the provenance line,
    then the fact sheet in a fenced block. Only the prose is the model's; the
    rest is reconstructed on the way back in.
    """
    body = page.split("\n---\n", 1)[0]
    lines = body.split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _written_by_of(page: str) -> str:
    match = re.search(r"written by ([^\s.]+)", page)
    return match.group(1) if match else ""


def status() -> int:
    from kb import store as kbstore
    ok, why = kbstore.ready()
    if not ok:
        print(why, file=sys.stderr)
        return 1
    db = kbstore.facts(read_only=False)
    rows = store.status(db)
    print()
    print(f"  {'level':<10}{'pages':>7}{'facts only':>12}{'tok in':>10}"
          f"{'tok out':>9}")
    print("  " + "-" * 50)
    for r in rows:
        print(f"  {r['scope']:<10}{r['pages']:>7}{r['facts_only'] or 0:>12}"
              f"{r['tokens_in'] or 0:>10,}{r['tokens_out'] or 0:>9,}")
    if not rows:
        print("  nothing written yet")
    print()
    for scope in ORDER:
        units = _units(db, scope)
        have = {r["key"] for r in db.execute(
            "SELECT key FROM summary WHERE scope=?", (scope,))}
        missing = [k for k, _l in units if k not in have]
        if missing:
            print(f"  {scope}: {len(missing)} without a page"
                  f" - {', '.join(missing[:6])}"
                  f"{'...' if len(missing) > 6 else ''}")
    print()
    db.close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("--level", choices=ORDER, help="only this level")
    ap.add_argument("--limit", type=int, help="only the first N of each level")
    ap.add_argument("--refresh", action="store_true",
                    help="rewrite pages even when they are still fresh")
    ap.add_argument("--dry-run", action="store_true",
                    help="build every prompt and print its size, call nothing")
    ap.add_argument("--estimate", action="store_true",
                    help="what the run will cost, from measured throughput")
    ap.add_argument("--import-pages", action="store_true",
                    help="rebuild the summary table from the pages on disk")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)

    if args.import_pages:
        return import_pages()
    if args.status:
        return status()
    if args.estimate:
        from kb import store as kbstore
        ok, why = kbstore.ready()
        if not ok:
            print(why, file=sys.stderr)
            return 1
        db = kbstore.facts(read_only=False)
        estimate(db)
        db.close()
        return 0
    return build(args.level, args.limit, args.refresh, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
