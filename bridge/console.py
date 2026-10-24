"""A menu on top of the existing commands, for people who do not want to remember them.

    python -m bridge.console

Every entry runs one of the commands from the README as a child process, so the menu adds no behaviour of its own.
Executing a fix always asks for a typed confirmation first. Only the kill switch and the audit log are read directly.
"""
import subprocess
import sys

from actions import audit, killswitch

PYTHON = sys.executable


def _run(*module_args: str) -> None:
    subprocess.run([PYTHON, "-m", *module_args], check=False)


def _kill_state() -> str:
    return "ENGAGED" if killswitch.is_engaged() else "off"


def _ask_symptom(ask) -> list[str]:
    symptom = ask("Symptom (blank for the default): ").strip()
    return ["--symptom", symptom] if symptom else []


def _investigate(ask, execute: bool) -> None:
    if execute:
        if killswitch.is_engaged():
            print("The kill switch is engaged, so nothing can run. Clear it first.")
            return
        if ask("This will really run the approved command. Type EXECUTE to continue: ").strip() != "EXECUTE":
            print("cancelled")
            return
    _run("bridge.run", *(["--execute"] if execute else []), *_ask_symptom(ask))


def _kill_menu(ask) -> None:
    choice = ask("Kill switch [on/off/status]: ").strip().lower()
    if choice in ("on", "off", "status"):
        _run("actions.kill", choice)
    else:
        print("unknown choice")


def _audit_tail(limit: int = 15) -> None:
    entries = audit.read()[-limit:]
    if not entries:
        print("the audit log is empty")
    for e in entries:
        print(f"{e['ts']}  {e.get('incident_id') or '-':<20} {e['event']:<24} {e.get('action_id') or ''} {e.get('outcome') or ''}")


def menu(ask=input) -> int:
    while True:
        print(f"\n=== Incident Zero   (kill switch: {_kill_state()}) ===")
        print(" 1  Check backends")
        print(" 2  Snapshot of the system")
        print(" 3  Investigate and propose a fix (dry run)")
        print(" 4  Investigate and fix (really executes, asks to confirm)")
        print(" 5  Kill switch")
        print(" 6  Recent audit log")
        print(" q  Quit")
        try:
            choice = ask("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == "1":
            _run("tools.check_backends")
        elif choice == "2":
            _run("tools.snapshot")
        elif choice == "3":
            _investigate(ask, execute=False)
        elif choice == "4":
            _investigate(ask, execute=True)
        elif choice == "5":
            _kill_menu(ask)
        elif choice == "6":
            _audit_tail()
        elif choice in ("q", "quit", "exit"):
            return 0
        else:
            print("choose 1-6 or q")


if __name__ == "__main__":
    sys.exit(menu())
