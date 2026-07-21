"""Frozen-app entry point (the script PyInstaller compiles into Daylook.app).

One binary, two roles, chosen by the first argument:

  * ``--capture`` -> run the real screen-capture child (daylook.capture_script.real_capture).
    The Root Agent re-launches this same bundle with ``--capture`` when the user says "start
    tracking"; a frozen app can't do ``python -m module``, so this marker is how we get there.
  * anything else -> the normal launcher (server + browser). ``--smoke`` / ``--no-browser`` flow
    straight through to launcher.main.

macOS sometimes passes a ``-psn_<n>`` process-serial-number argument on double-click; we drop it
so argparse downstream doesn't choke.
"""

import sys


def _clean_argv(args):
    return [a for a in args if not a.startswith("-psn_")]


def main() -> int:
    args = _clean_argv(sys.argv[1:])

    if args and args[0] == "--capture":
        # Hand the rest of the args (--minutes/--dry-run/...) to real_capture's own argparse.
        from daylook.capture_script import real_capture
        sys.argv = [sys.argv[0]] + args[1:]
        real_capture.main()
        return 0

    from daylook.launcher import main as launcher_main
    return launcher_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
