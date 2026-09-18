"""Repo-relative import bootstrap for the godSeye test suite.

WHY THIS FILE EXISTS
--------------------
Seven test modules import the real AirSim PythonClient (``import airsim``):
test_fake_airsim, test_geo_gate, test_launch, test_missions, test_realism,
test_server and test_targets.  Before this file, exactly three places in the
suite -- test_bridge.py, test_fake_airsim.py and test_geo_gate.py -- carried

    sys.path.insert(0, "/Users/<someone>/.../airsim/PythonClient")

at module scope, and the other five carried nothing.  The full run passed only
by accident of alphabetical order: ``test_bridge.py`` sorts first, ran that
insert as a side effect of being imported, and every later module in the same
process inherited a mutated ``sys.path``.  Collect any of the silent modules on
its own -- ``pytest tests/test_server.py`` -- and it died with
``ModuleNotFoundError: No module named 'airsim'``; so did test_launch,
test_missions, test_realism and test_targets.  An absolute path to one
developer's home directory is also not a thing CI can ever satisfy.

WHAT IT DOES
------------
1. Puts the in-repo ``mcp/`` source tree on ``sys.path`` so ``godseye_uav``
   imports without the package having been pip-installed.
2. Resolves the AirSim PythonClient RELATIVE to this repo (it is a sibling
   checkout of microsoft/airsim, not a pip package) and puts it on
   ``sys.path`` for every test module, whatever order they run in.
3. If no REAL client can be found, PREPENDS a meta-path hook so that the *first*
   attempt to ``import airsim`` fails with a message naming what is missing and
   every path that was searched -- instead of the bare ModuleNotFoundError
   above, and instead of a hollow namespace package (see
   ``_environment_supplies_airsim`` and ``_MissingAirSimFinder``, which explain
   why "real" and "prepend" are both load-bearing words in this sentence).

Point 3 is deliberately a loud error and NOT a skip: the AirSim client is what
the bridge/server contract tests are testing against, and a suite that quietly
drops those modules would report green while testing nothing.  Test modules
that do not need AirSim (test_geo, test_safety, test_store, ...) are unaffected,
because the hook only fires on an actual ``import airsim``.

Every branch here is exercised by ``scripts/ci.sh``'s "seam gate", which runs
conftest against a throwaway repo skeleton so the developer's own sibling
checkout cannot answer for it.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import sys

#: Repo root -- this file is <repo>/tests/conftest.py.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Where `godseye_uav` lives in the source tree.
MCP_SRC = REPO_ROOT / "mcp"

#: Env var an operator (or CI) can set to point at a PythonClient elsewhere.
AIRSIM_ENV_VAR = "GODSEYE_AIRSIM_PYTHONCLIENT"

#: Candidate PythonClient locations, in priority order, expressed relative to
#: the repo.  The second is where `scripts/ci.sh` fetches a pinned copy; it
#: lives under the already-gitignored `.godseye/` so a CI run never dirties the
#: working tree.  (ci.sh also exports AIRSIM_ENV_VAR, so this is a fallback for
#: a bare `pytest` run on a machine ci.sh has already primed.)
_AIRSIM_RELATIVE_CANDIDATES = (
    REPO_ROOT.parent / "airsim" / "PythonClient",  # sibling checkout (dev box)
    REPO_ROOT / ".godseye" / "vendor" / "airsim" / "PythonClient",  # ci.sh fetch
)


def _is_pythonclient(path: pathlib.Path) -> bool:
    """True only if `path` really is an AirSim PythonClient root.

    Checked by the file that would actually be imported, not by directory
    existence: an empty `../airsim/PythonClient` left behind by a half-finished
    clone must NOT be accepted as a valid source and then fail later with a
    confusing error from inside a test.
    """
    return (path / "airsim" / "__init__.py").is_file()


def _prepend(path: pathlib.Path) -> None:
    """Put `path` first on sys.path (idempotent).

    Front of the path on purpose: the in-repo checkout is the client the
    contract tests are pinned against and must win over any stale
    `pip install airsim` in the environment.
    """
    text = str(path)
    while text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)


def _resolve_airsim_pythonclient() -> tuple[pathlib.Path | None, list[str]]:
    """Find the AirSim PythonClient. Returns (path or None, paths tried)."""
    tried: list[str] = []

    override = os.environ.get(AIRSIM_ENV_VAR)
    if override is not None and not override.strip():
        # Set-but-blank is an operator mistake (`export VAR=` / `VAR="$UNSET"`),
        # not a request for the default.  Treating it as "unset" would fall
        # through to the sibling checkout AND print "(unset)" in the searched
        # list, i.e. answer from a source the operator did not choose and then
        # misreport why.
        raise RuntimeError(
            f"{AIRSIM_ENV_VAR} is set but empty. Unset it to use the sibling "
            f"checkout, or point it at an AirSim PythonClient directory."
        )
    if override:
        candidate = pathlib.Path(override).expanduser()
        if not candidate.is_absolute():
            candidate = (REPO_ROOT / candidate).resolve()
        if _is_pythonclient(candidate):
            return candidate, tried
        # An explicit override that does not hold a PythonClient is an operator
        # error, not something to quietly paper over with a fallback: say so now
        # rather than letting a stale sibling checkout answer in its place.
        raise RuntimeError(
            f"{AIRSIM_ENV_VAR}={override!r} does not contain an AirSim "
            f"PythonClient: expected {candidate / 'airsim' / '__init__.py'} "
            f"to exist. Unset {AIRSIM_ENV_VAR} to use the sibling checkout, or "
            f"point it at a real PythonClient directory."
        )
    tried.append(f"${AIRSIM_ENV_VAR} (unset)")

    for candidate in _AIRSIM_RELATIVE_CANDIDATES:
        if _is_pythonclient(candidate):
            return candidate, tried
        tried.append(f"{candidate} (no airsim/__init__.py)")

    return None, tried


def _environment_supplies_airsim() -> bool:
    """True only if the ambient environment supplies a REAL `airsim` package.

    ``importlib.util.find_spec("airsim") is not None`` is NOT that test.  A
    directory named ``airsim`` with no ``__init__.py`` anywhere on ``sys.path``
    makes find_spec return a NAMESPACE spec (loader=None, origin=None), and
    ``import airsim`` then succeeds as an empty module with no
    ``MultirotorClient`` on it.  That is not academic here: the AirSim checkout's
    ROOT directory is itself named ``airsim`` (it holds ``PythonClient/``), so
    the single most likely operator mistake -- putting the checkout's PARENT on
    PYTHONPATH, which is the habit this file replaces -- produces exactly that
    hollow module.  Accepting it would disarm the loud hook below and trade a
    clear "AirSim PythonClient not found" for an AttributeError raised deep
    inside a test.

    So: a spec counts only when it has a real ``origin`` on disk, which is what
    an actual package or module has and a namespace package never does.
    """
    try:
        spec = importlib.util.find_spec("airsim")
    except (ImportError, ValueError):
        return False
    if spec is None or spec.origin is None:
        return False
    # "frozen"/"built-in" have no filesystem origin; neither is a PythonClient.
    return os.path.isfile(spec.origin)


def _missing_airsim_message(tried: list[str]) -> str:
    sibling = REPO_ROOT.parent / "airsim"
    fetched = REPO_ROOT / ".godseye" / "vendor" / "airsim"
    lines = [
        "The godSeye test suite needs the AirSim PythonClient, and it was not found.",
        "",
        "It is a SIBLING CHECKOUT of this repo, not a pip package. Get it with:",
        f"    git clone https://github.com/microsoft/airsim {sibling}",
        (
            f"or run {REPO_ROOT / 'scripts' / 'ci.sh'}, which fetches a pinned "
            f"copy into {fetched}."
        ),
        "",
        "Searched, in order, for a directory containing airsim/__init__.py:",
    ]
    lines += [f"  - {t}" for t in tried]
    lines += [
        (
            "  - the interpreter's own sys.path (no importable 'airsim' package "
            "there either; a bare directory named 'airsim' with no "
            "__init__.py does NOT count)"
        ),
        "",
        (
            f"Set {AIRSIM_ENV_VAR}=/path/to/PythonClient to use a checkout "
            "somewhere else."
        ),
    ]
    return "\n".join(lines)


def _real_airsim_on_sys_path() -> bool:
    """True if some sys.path entry holds a genuine `airsim` package.

    Scans sys.path directly instead of calling importlib.util.find_spec, which
    would re-enter the finder below.  `<entry>/airsim/__init__.py` is the layout
    of BOTH the AirSim PythonClient checkout and a `pip install airsim`, and it
    is precisely what a namespace-package directory lacks.
    """
    for entry in list(sys.path):
        try:
            if _is_pythonclient(pathlib.Path(entry or ".")):
                return True
        except (OSError, ValueError):
            # A zip/egg entry or a path with embedded nulls is not a checkout;
            # narrow on purpose -- anything else should surface, not be eaten.
            continue
    return False


class _MissingAirSimFinder:
    """sys.meta_path hook that turns `import airsim` into a useful error.

    PREPENDED, not appended.  Appending is the obvious choice and it is wrong
    here: a bare directory named `airsim` on sys.path makes the stdlib
    PathFinder return a NAMESPACE spec, PathFinder is consulted first, and an
    appended hook therefore never runs -- `import airsim` quietly yields an
    empty module and the failure resurfaces later as an AttributeError inside a
    test.  Prepending is safe because this hook is only armed after
    `_environment_supplies_airsim()` has already established that no real
    `airsim` exists anywhere, so there is nothing legitimate for it to shadow.

    It is still not unconditional: sys.path can grow a real PythonClient after
    arming (a test module's own insert), so every call re-checks and steps aside
    for a genuine package.  It raises rather than returning None so the message
    reaches the collector verbatim.
    """

    def __init__(self, message: str) -> None:
        self._message = message

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != "airsim" and not fullname.startswith("airsim."):
            return None
        if _real_airsim_on_sys_path():
            return None  # a real client turned up; let the stdlib import it
        raise ModuleNotFoundError(self._message, name=fullname)


def _bootstrap() -> None:
    if (MCP_SRC / "godseye_uav" / "__init__.py").is_file():
        _prepend(MCP_SRC)

    client_root, tried = _resolve_airsim_pythonclient()
    if client_root is not None:
        _prepend(client_root)
        return

    # Nothing repo-relative. If the environment already provides a real `airsim`
    # (PYTHONPATH, a pip install), leave it alone; otherwise arm the hook.
    if _environment_supplies_airsim():
        return

    sys.meta_path.insert(0, _MissingAirSimFinder(_missing_airsim_message(tried)))


_bootstrap()
