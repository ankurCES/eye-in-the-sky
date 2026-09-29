"""Real-system token check for simulated wargame output (M14a; WG spec §5.4 B1, V6).

`assert_no_real_system_tokens(obj)` walks every string in `obj` (dict keys and
values, lists, tuples, sets, dataclass fields) and fails on a real air-defence
system designation or a weaponeering term. Wargame labels, notes, tool results,
graph rows and the AAR must be generic and notional (D1).

Matching is case-insensitive, so an OB keyword ("tor", "pantsir", "s-300")
leaking in lower case is caught as well. "munition" is matched as a word start,
so the ordinary word "ammunition" ("Shooter active with ammunition") passes.
"""
from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator, Mapping
from typing import Any

#: (name, pattern). The spec's list, in order.
TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx, re.IGNORECASE)) for name, rx in (
        ("S-300", r"S-300"),
        ("SA-n", r"\bSA-\d"),
        ("Tor", r"\bTor\b"),
        ("Pantsir", r"Pantsir"),
        ("ZSU", r"ZSU"),
        ("ZU-", r"\bZU-"),
        ("warhead", r"warhead"),
        ("munition", r"\bmunition"),
        ("fuze", r"fuze"),
        ("fuzing", r"fuzing"),
        ("blast radius", r"blast radius"),
        ("aimpoint", r"aimpoint"),
        ("CEP", r"\bCEP\b"),
        ("kg/mm", r"\d+\s?(kg|mm)\b"),
    ))


def iter_strings(obj: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    """Every `(path, string)` in `obj`, keys included."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, bytes):
        yield path, obj.decode("utf-8", "replace")
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            yield from iter_strings(getattr(obj, f.name), f"{path}.{f.name}")
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            yield from iter_strings(k, f"{path}{{key {k!r}}}")
            yield from iter_strings(v, f"{path}[{k!r}]")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from iter_strings(v, f"{path}[{i}]")
    elif isinstance(obj, (set, frozenset)):
        for v in sorted(obj, key=repr):
            yield from iter_strings(v, f"{path}{{}}")


def find_real_system_tokens(obj: Any) -> list[tuple[str, str, str]]:
    """Every hit as `(path, token name, the string)`."""
    hits = []
    for path, text in iter_strings(obj):
        for name, rx in TOKEN_PATTERNS:
            if rx.search(text):
                hits.append((path, name, text))
    return hits


def assert_no_real_system_tokens(obj: Any) -> None:
    """Fail when any string in `obj` carries a real-system or weaponeering token."""
    hits = find_real_system_tokens(obj)
    if hits:
        lines = "\n".join(f"  {p}: {n!r} in {t[:120]!r}" for p, n, t in hits[:20])
        raise AssertionError(
            f"{len(hits)} real-system token(s) in simulated wargame output:\n{lines}")
