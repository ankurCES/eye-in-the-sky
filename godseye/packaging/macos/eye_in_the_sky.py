"""PyInstaller entry point for "Eye in the Sky.app" (packaging/macos/EyeInTheSky.spec).

Kept tiny and free of relative imports: PyInstaller runs it as ``__main__``.
Everything else is ``godseye_uav.app.main`` (the same entry as
``python -m godseye_uav.app``). Frozen, that entry finds the UI in
``sys._MEIPASS/ui`` and the Claude CLI in ``Contents/Helpers/claude``.
"""
import sys

from godseye_uav.app import main


def _argv() -> list[str]:
    # Older LaunchServices passed a process serial number (-psn_0_12345) to
    # apps started from Finder; it is not ours to parse.
    return [a for a in sys.argv[1:] if not a.startswith("-psn_")]


if __name__ == "__main__":
    sys.exit(main(_argv()))
