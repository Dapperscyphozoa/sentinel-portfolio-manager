"""Repo-wide lint: no strategy may reach into BusClient PRIVATE internals.

Sentinel-audit-driven 2026-05-19. The previous regression test was
function-scoped (only checked hl_vault_predict.evaluate). It missed two
cases:

  1. A new strategy could reintroduce bus._client / bus.base_url /
     bus.timeout access — not caught.
  2. A future refactor in hl_vault_predict could move the offending
     access to a helper method outside evaluate() — not caught.

This test scans EVERY file in strategy_runner/strategies/ and fails if
any private-bus access appears in actual code (comments and docstrings
are stripped). Only the BusClient class itself in common/bus_client.py
is permitted to access these attributes.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path


PRIVATE_BUS_ATTRS = ["bus._client", "bus.base_url", "bus.timeout"]


def _strip_comments_and_docstrings(src: str) -> str:
    """Return source with comments removed and docstrings stripped via AST
    re-parse + unparse. Robust against indentation."""
    # Remove # comments line-by-line first
    lines = []
    for line in src.splitlines():
        # find first # that's not inside a string — for simplicity skip lines
        # starting with leading whitespace and #, plus inline # after code
        # we use a simple state machine
        in_str = False
        quote = None
        esc = False
        out_chars = []
        for ch in line:
            if esc:
                out_chars.append(ch); esc = False; continue
            if ch == "\\":
                out_chars.append(ch); esc = True; continue
            if in_str:
                out_chars.append(ch)
                if ch == quote:
                    in_str = False
                continue
            if ch in ('"', "'"):
                in_str = True; quote = ch
                out_chars.append(ch); continue
            if ch == "#":
                break
            out_chars.append(ch)
        lines.append("".join(out_chars).rstrip())
    no_comments = "\n".join(lines)

    # Strip docstrings via AST round-trip
    try:
        tree = ast.parse(no_comments)
    except SyntaxError:
        return no_comments

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_no_strategy_uses_private_bus_attrs():
    """Scan every strategy file. None may reference bus._client,
    bus.base_url, or bus.timeout in code (after stripping docstrings/
    comments)."""
    repo_root = Path(__file__).parent.parent
    strategies_dir = repo_root / "strategy_runner" / "strategies"
    assert strategies_dir.is_dir(), f"missing {strategies_dir}"

    violations = []
    for py_file in sorted(strategies_dir.glob("*.py")):
        # Skip __init__ and _base
        if py_file.name in ("__init__.py", "_base.py"):
            continue
        src = py_file.read_text()
        code_only = _strip_comments_and_docstrings(src)
        for attr in PRIVATE_BUS_ATTRS:
            if attr in code_only:
                # Find the line number in the ORIGINAL source for diagnostics
                for ln_idx, ln in enumerate(src.splitlines(), 1):
                    # Skip likely comment/docstring lines
                    stripped = ln.lstrip()
                    if stripped.startswith("#"):
                        continue
                    if attr in ln:
                        violations.append((py_file.name, ln_idx, attr, ln.strip()))
                        break

    assert not violations, (
        "Strategies must use BusClient PUBLIC methods (e.g. bus.hlp_position(coin)) "
        "instead of reaching into private attrs. Violations:\n"
        + "\n".join(f"  {f}:{ln} — {a}: {snippet}" for f, ln, a, snippet in violations)
    )


def test_helper_strips_comments():
    """Sanity: the comment/docstring stripper works."""
    src = (
        'def f():\n'
        '    """This mentions bus._client in the docstring."""\n'
        '    # also bus._client in a comment\n'
        '    return None\n'
    )
    out = _strip_comments_and_docstrings(src)
    assert "bus._client" not in out, f"stripper missed: {out!r}"


def test_helper_keeps_real_code():
    src = (
        'def f(bus):\n'
        '    x = bus._client\n'
        '    return x\n'
    )
    out = _strip_comments_and_docstrings(src)
    assert "bus._client" in out
