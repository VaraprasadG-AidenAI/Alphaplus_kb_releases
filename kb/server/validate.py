"""
Check a tool call against its own declared schema, before it runs.

    bad = validate.check(tool_declaration, arguments)   # "" when fine

## Why this exists

There was no validation at all.  `handler(**arguments)` was the entire
validator, which means Python's own signature check was doing the work, and it
only catches two things: a missing required argument and an unknown keyword.
Everything the schema actually says was decoration.

Three consequences, all of them measured on the shipped surface:

  * `who_touches_file(mode="wrote")` answered the `mode="any"` question.  The
    implementation tests `== "write"` and `== "read"` and falls through, so a
    typo in the one argument that decides read-versus-write returned the wrong
    answer with no indication.  `where_is_field_used(direction=...)` is the
    same shape.
  * `read_member(count=100000)` was accepted.  The description said "max 400"
    and the clamp was in the code, so the declared contract and the enforced
    one were different documents.
  * `trace_ticket(limit=...)` and `program_structure(max_rows=...)` had no
    bound at either end.

## Why not a JSON Schema library

Two reasons, and the second is the real one.

It would be a dependency, and while `requirements.txt` records that the
dependency-free property has already been spent on psycopg and neo4j, spending
it again needs a better reason than 80 lines of code.

More importantly: a general validator silently accepts keywords it does not
implement.  Declare `"pattern"` or `"minLength"` against a general library and
it is enforced; declare it against a partial one and it is ignored, which is
indistinguishable from working.  So this supports exactly five keywords -
`type`, `required`, `enum`, `minimum`, `maximum` - and `selftest()` asserts
that no declared schema uses any other.  A schema this cannot enforce is a
test failure, not a silent pass.

## What the message has to do

An MCP tool-execution error is recoverable by the agent, so the message is
written for one: it names the argument, what it got, and what would be
accepted.  For an enum that means listing the values, because "invalid mode" a
turn before "invalid mode" again is the failure this is meant to prevent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: The only keywords `check` understands.  `selftest` refuses a schema that
#: uses anything else, so this list and the declarations cannot drift.
SUPPORTED = frozenset({"type", "description", "enum", "minimum", "maximum",
                       "default"})

#: JSON Schema type -> what Python types satisfy it.  `bool` is deliberately
#: excluded from `integer`: True would otherwise pass as 1 and reach SQL.
_TYPES: Dict[str, tuple] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list, tuple),
}


def _type_ok(value: Any, declared: str) -> bool:
    allowed = _TYPES.get(declared)
    if allowed is None:
        return True                     # a type we do not model: let it pass
    if declared in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, allowed)


def check(tool: Optional[Dict[str, Any]], args: Dict[str, Any]) -> str:
    """"" when the call is acceptable, else a message written for the agent.

    A missing declaration passes: `handle()` has already resolved the handler,
    so an absent entry means the two dicts disagree, and that is `selftest`'s
    job to catch rather than a reason to refuse a call that would work.
    """
    if not tool:
        return ""
    schema = tool.get("inputSchema") or {}
    props: Dict[str, Any] = schema.get("properties") or {}
    required: List[str] = list(schema.get("required") or [])
    name = tool.get("name", "the tool")
    args = args or {}
    problems: List[str] = []

    for key in required:
        if key not in args or args[key] is None or args[key] == "":
            problems.append(f"{key} is required and was not given")

    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            known = ", ".join(sorted(props)) or "none"
            problems.append(f"{key} is not an argument of {name}"
                            f" (it takes: {known})")
            continue
        if value is None:
            continue

        declared = str(spec.get("type") or "")
        if declared and not _type_ok(value, declared):
            got = type(value).__name__
            problems.append(f"{key} must be {declared}, got {got}"
                            f" ({value!r})")
            continue

        allowed = spec.get("enum")
        if allowed and value not in allowed:
            problems.append(f"{key}={value!r} is not one of: "
                            + ", ".join(repr(a) for a in allowed))
            continue

        # Bounds.  Reported rather than clamped: a caller that asked for 4,000
        # rows and silently got 200 cannot tell the answer was cut, and this
        # surface's whole contract is that a partial answer says so.
        low, high = spec.get("minimum"), spec.get("maximum")
        if low is not None and isinstance(value, (int, float)) and value < low:
            problems.append(f"{key}={value} is below the minimum of {low}")
        if high is not None and isinstance(value, (int, float)) and value > high:
            problems.append(f"{key}={value} is above the maximum of {high}"
                            + (f" - call again with {key}={high}"
                               if high else ""))

    if not problems:
        return ""
    head = (f"{name} was called with arguments it cannot accept. Nothing ran, "
            f"so nothing is half-done.")
    body = "\n".join(f"  - {p}" for p in problems)
    return head + "\n" + body


def selftest() -> int:
    """Assert every declared schema is one this module can actually enforce.

    This is the check that keeps the module honest.  A schema keyword nobody
    validates looks exactly like a schema keyword that passes.
    """
    from . import server as rpc

    bad: List[str] = []
    for tool in rpc.TOOLS:
        schema = tool.get("inputSchema") or {}
        for key in schema:
            if key not in ("type", "properties", "required",
                           "additionalProperties"):
                bad.append(f"{tool['name']}: schema keyword {key!r}")
        for prop, spec in (schema.get("properties") or {}).items():
            for key in spec:
                if key not in SUPPORTED:
                    bad.append(f"{tool['name']}.{prop}: keyword {key!r} is "
                               f"declared and NOT enforced")

    cases = [
        ("who_touches_file", {"file": "CUSMAS", "mode": "wrote"}, True),
        ("who_touches_file", {"file": "CUSMAS", "mode": "write"}, False),
        ("who_touches_file", {"mode": "write"}, True),
        ("who_touches_file", {"file": "CUSMAS", "nope": 1}, True),
        ("read_member", {"name": "ORD100", "count": 100000}, True),
        ("read_member", {"name": "ORD100", "count": 400}, False),
        ("read_member", {"name": "ORD100", "count": "400"}, True),
        ("what_uses", {"name": "CUSMAS", "kind": "defines"}, False),
        ("find_paragraph", {"name": "VALIDATE"}, False),
    ]
    by_name = {t["name"]: t for t in rpc.TOOLS}
    fails = 0
    print("  argument validation")
    for tool_name, args, should_fail in cases:
        got = check(by_name.get(tool_name), args)
        ok = bool(got) == should_fail
        fails += 0 if ok else 1
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {tool_name}({args}) -> "
              + (got.splitlines()[1].strip() if got else "accepted"))

    print()
    if bad:
        print(f"  FAIL {len(bad)} unenforceable schema declaration(s):")
        for line in bad:
            print(f"       {line}")
    else:
        print(f"  ok   every declared keyword across {len(rpc.TOOLS)} tools "
              f"is enforced")
    return 1 if (fails or bad) else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
