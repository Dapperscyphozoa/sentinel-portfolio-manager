#!/usr/bin/env python3
"""Run promotion_gate against the live PM registry. Prints audit table.

Exit code:
    0  all engines pass
    1  one or more engines fail (would block boot in strict mode)

Usage:
    python3 scripts/audit_promotion_gate.py [--strict]
"""
from __future__ import annotations

import sys
import os

# allow run from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pm.pretrade asserts cap_frac sum == 1.0 at import. For audit-only we want
# to see ALL registry rows including any current drift, so import safely.
try:
    from pm.pretrade import ENGINE_REGISTRY
except AssertionError:
    # Re-import by stripping the offending assertion: parse + exec just the
    # registry dict. Cheap and avoids touching pretrade.py.
    import ast, pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "pm" / "pretrade.py"
    tree = ast.parse(src.read_text())
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(getattr(t, "id", None) == "ENGINE_REGISTRY" for t in targets):
                ENGINE_REGISTRY = ast.literal_eval(node.value)
                break
    else:
        raise
from pm.promotion_gate import audit, format_table


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    rows = audit(ENGINE_REGISTRY)
    print(format_table(rows))
    failed = [r for r in rows if not r["ok"]]
    print()
    print(f"summary: {len(rows)} engines, {len(rows) - len(failed)} pass, {len(failed)} fail")
    if failed:
        names = ", ".join(r["name"] for r in failed)
        print(f"would-demote: {names}")
        if strict:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
