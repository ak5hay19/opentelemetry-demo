"""The only place that changes the system. Dry run is the default.

`runner` has the same shape as subprocess.run so tests can pass a fake and never touch Docker.
"""
import subprocess

from actions import audit, killswitch, registry
from tools.containers import container_health
from tools.result import iso, utc_now

COMMAND_TIMEOUT_S = 60
KILL_TEXT = "kill switch engaged"


def _result(mode, ok, started, detail, error=None) -> dict:
    return {"ok": ok, "mode": mode, "started_at": iso(started), "ended_at": iso(utc_now()),
            "detail": detail, "error": error}


def execute(proposal, *, dry_run=True, runner=None, health=container_health, actions_dir=None,
            timeout=COMMAND_TIMEOUT_S) -> dict:
    started = utc_now()
    runner = runner or subprocess.run
    info = proposal if isinstance(proposal, dict) else {}
    base = {"incident_id": info.get("incident_id"), "action_id": info.get("action_id"),
            "params": info.get("params"), "directory": actions_dir}
    mode = "dry_run" if dry_run else "executed"

    def blocked(reason: str) -> dict:
        audit.record("blocked", mode="blocked", outcome=reason, **base)
        return _result("blocked", False, started, "not executed", reason)

    if killswitch.is_engaged(actions_dir):
        return blocked(f"{KILL_TEXT}; nothing was run")
    command, why = registry.validate(proposal, health)
    if command is None:
        return blocked(f"proposal rejected: {why}")

    if dry_run:
        audit.record("dry_run", mode=mode, outcome="nothing changed", **base)
        return _result(mode, True, started, f"dry run, nothing changed. Would run: {' '.join(command)}")

    already = [e for e in audit.read(actions_dir)
               if e.get("incident_id") == info["incident_id"] and e.get("event") == "execute_started"]
    if already:
        return blocked("this incident already had its one action; a second one is not allowed")
    if killswitch.is_engaged(actions_dir):  # checked again right before the command
        return blocked(f"{KILL_TEXT}; nothing was run")
    try:
        audit.record("execute_started", mode=mode, outcome=" ".join(command), **base)
    except OSError as exc:
        return _result("blocked", False, started, "not executed", f"audit log not writable, so nothing was run: {exc}")

    try:
        done = runner(command, capture_output=True, text=True, timeout=timeout)
        error = None if done.returncode == 0 else f"{' '.join(command)} exited {done.returncode}: {(done.stderr or '').strip()[:200]}"
    except subprocess.TimeoutExpired:
        error = f"{' '.join(command)} timed out after {timeout}s"
    except OSError as exc:
        error = f"cannot run docker: {exc}"
    audit.record("execute_finished", mode=mode, outcome=error or "ok", **base)
    if error:
        return _result(mode, False, started, f"ran: {' '.join(command)}", error)
    return _result(mode, True, started, f"ran: {' '.join(command)}")
