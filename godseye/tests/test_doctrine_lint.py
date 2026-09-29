"""Doctrine lint for M14a (PLAN.md §4.5a; WG v2 spec §5.1, unit B0).

M14a amends the old ISR-only rule: ISR stays the default, and an opt-in,
operator-approved simulated wargame engages simulated scenario units only.
The amendment lands in the docs FIRST, so no later unit can ship wargame code
while a doc still says the rule forbids it, or quietly drop the qualifier.

The lint walks the repository, skipping `tests/`, `node_modules`, `dist`,
`build`, `.beads`, `.venv`, `.freya`, `__pycache__` and `.git`, and reads every
`*.md`, `*.py`, `*.js` and `*.yaml` file. It fails on:

* any line containing "none may be added" (case-insensitive) without "M14a"
  within three lines either side, because every such prohibition must now
  cite the rule that scopes it;
* the `godseye-uav` skill's `SKILL.md` lacking the sentence that keeps the
  `wg_*` tools out of its scope (the skill stays ISR-only, D3);
* `PLAN.md` lacking the "### 4.5a M14a" section.

It also pins `threat.ISR_AUTHORITY_NOTE` and `realdata.MAPPED_DATA_CAVEAT` to
their HEAD literals: every ISR output quotes them and both stay true under
M14a (aircraft never deliver effects; mapped data is never a target).

Pure file reads: no network, no server, no ports.
"""
from __future__ import annotations

import os
import pathlib

from godseye_uav import realdata, threat

#: Repository root: `godseye/tests/test_doctrine_lint.py` -> repo.
REPO = pathlib.Path(__file__).resolve().parents[2]

#: Directory names never walked (§5.1, plus `.git`, which holds no source).
SKIP_DIRS = frozenset({
    "tests", "node_modules", "dist", "build", ".beads", ".venv", ".freya",
    "__pycache__", ".git",
})
#: File types the lint reads (§5.1).
SUFFIXES = (".md", ".py", ".js", ".yaml")

#: The prohibition phrase, matched case-insensitively.
PHRASE = "none may be added"
#: The rule every prohibition must cite, matched as written.
RULE = "M14a"
#: Lines either side of a hit that may carry the rule.
WINDOW = 3

#: The sentence §5.1 adds to the ISR skill (after its ISR-only paragraph).
SKILL_SENTENCE = (
    "wg_* tools (present only when the host runs with --wargame-mcp) are out of "
    "scope for this skill; never call them."
)
#: The skill that stays ISR-only, by its frontmatter name.
SKILL_NAME = "godseye-uav"
#: The heading PLAN.md must carry.
PLAN_HEADING = "### 4.5a M14a"

#: HEAD literals (commit 3fd034e) of the two ISR statements M14a keeps.
HEAD_ISR_AUTHORITY_NOTE = (
    "ISR-only: this is a sensor-posture and self-protection advisory. "
    "godSeye has no engagement capability and confers no engagement authority; "
    "command decisions remain with the operator."
)
HEAD_MAPPED_DATA_CAVEAT = (
    "MAPPED DATA, NOT AN ORDER OF BATTLE: sites come from OpenStreetMap/Overpass "
    "mapped features. The coverage is incomplete, the tagging is unverified, and "
    "nothing here is confirmed by observation. Use as ISR context only (M14); do "
    "not report it as a confirmed order of battle."
)


def walk_doctrine_files(root: pathlib.Path = REPO) -> list[pathlib.Path]:
    """Every lintable file under `root`, skipping `SKIP_DIRS` (sorted)."""
    found: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            if name.endswith(SUFFIXES):
                found.append(pathlib.Path(dirpath) / name)
    return sorted(found)


def read_lines(path: pathlib.Path) -> list[str]:
    """The file's lines; undecodable bytes are replaced, never fatal."""
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def offending_lines(lines: list[str]) -> list[int]:
    """1-based numbers of lines with `PHRASE` and no `RULE` within `WINDOW`."""
    bad: list[int] = []
    for i, line in enumerate(lines):
        if PHRASE not in line.lower():
            continue
        lo, hi = max(0, i - WINDOW), min(len(lines), i + WINDOW + 1)
        if not any(RULE in lines[j] for j in range(lo, hi)):
            bad.append(i + 1)
    return bad


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(REPO).as_posix()


def _skill_files(files: list[pathlib.Path]) -> list[pathlib.Path]:
    """Every walked `SKILL.md` whose frontmatter names the ISR skill."""
    return [p for p in files
            if p.name == "SKILL.md"
            and f"name: {SKILL_NAME}" in read_lines(p)[:5]]


# ------------------------------------------------------------ the walker --

def test_walk_is_not_vacuous():
    """The lint must actually see the files it guards, or it proves nothing."""
    rels = {_rel(p) for p in walk_doctrine_files()}
    for must in ("README.md", "THIRD_PARTY_NOTICES.md", "godseye/PLAN.md",
                 "godseye/AGENTS.md", "godseye/CLAUDE.md",
                 "godseye/mcp/godseye_uav/threat.py",
                 "godseye/mcp/godseye_uav/server.py",
                 f"godseye/.agents/skills/{SKILL_NAME}/SKILL.md"):
        assert must in rels, f"doctrine lint no longer walks {must}"
    assert any(r.startswith("gods-eye-view/src/") and r.endswith(".js")
               for r in rels), "doctrine lint walks no console JS"
    assert not any(part in SKIP_DIRS for r in rels
                   for part in pathlib.PurePosixPath(r).parts[:-1])


def test_window_rule_on_fixtures():
    """The window is exactly three lines either side, and the phrase is
    matched case-insensitively while the rule is matched as written."""
    hit = "No kinetic tool exists, and None May Be Added."
    assert offending_lines([hit]) == [1]
    assert offending_lines([hit + " (M14a)"]) == []
    assert offending_lines(["M14a", "", "", hit]) == []
    assert offending_lines([hit, "", "", "M14a"]) == []
    assert offending_lines(["M14a", "", "", "", hit]) == [5]
    assert offending_lines([hit, "", "", "", "M14a"]) == [1]
    assert offending_lines([hit, "m14a"]) == [1]
    assert offending_lines(["nothing to see"]) == []


# ------------------------------------------------------------- the rules --

def test_every_none_may_be_added_cites_m14a():
    """Each "none may be added" prohibition carries M14a within 3 lines."""
    offenders = [f"{_rel(p)}:{n}"
                 for p in walk_doctrine_files()
                 for n in offending_lines(read_lines(p))]
    assert not offenders, (
        "'none may be added' without 'M14a' within 3 lines (PLAN.md §4.5a): "
        + ", ".join(offenders))


def test_isr_skill_keeps_wg_tools_out_of_scope():
    """The godseye-uav skill stays ISR-only and says wg_* is not for it."""
    skills = _skill_files(walk_doctrine_files())
    assert skills, f"no SKILL.md named {SKILL_NAME} was found"
    for path in skills:
        text = path.read_text(encoding="utf-8")
        assert SKILL_SENTENCE in text, (
            f"{_rel(path)} lacks the wg_* out-of-scope sentence")
        assert "**ISR-only.**" in text, f"{_rel(path)} lost its ISR-only rule"


def test_plan_has_the_m14a_section():
    """PLAN.md carries §4.5a M14a, between §4.5 and §4.6."""
    lines = read_lines(REPO / "godseye" / "PLAN.md")
    heads = [i for i, line in enumerate(lines) if line.startswith(PLAN_HEADING)]
    assert heads, f"PLAN.md lacks '{PLAN_HEADING}'"
    at = heads[0]
    before = [i for i, line in enumerate(lines) if line.startswith("### 4.5 ")]
    after = [i for i, line in enumerate(lines) if line.startswith("### 4.6 ")]
    assert before and after and before[0] < at < after[0]


def test_isr_statements_are_the_head_literals():
    """M14a keeps both ISR statements byte-identical."""
    assert threat.ISR_AUTHORITY_NOTE == HEAD_ISR_AUTHORITY_NOTE
    assert realdata.MAPPED_DATA_CAVEAT == HEAD_MAPPED_DATA_CAVEAT
