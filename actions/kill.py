"""Kill switch command line.

    python -m actions.kill on|off|status
"""
import argparse
import sys

from actions import audit, killswitch


def main(argv: list[str] | None = None, directory=None) -> int:
    parser = argparse.ArgumentParser(description="Engage, clear or show the action kill switch")
    parser.add_argument("command", choices=["on", "off", "status"])
    args = parser.parse_args(argv)

    if args.command == "on":
        killswitch.engage(directory)
        audit.record("kill_switch_engaged", directory=directory)
    elif args.command == "off":
        killswitch.clear(directory)
        audit.record("kill_switch_cleared", directory=directory)
    state = "ENGAGED (no action can run)" if killswitch.is_engaged(directory) else "off (actions allowed, still need approval)"
    print(f"kill switch: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
