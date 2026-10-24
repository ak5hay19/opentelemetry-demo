"""The only place that changes the system. Dry run is the default.

`runner` has the same shape as subprocess.run so tests can pass a fake and never touch Docker.
"""
import subprocess

from actions import audit, flag_adapter, killswitch, registry
from actions.remediations import RESTING_VARIANT
from tools.containers import container_health
from tools.result import iso, utc_now

COMMAND_TIMEOUT_S = 60
KILL_TEXT = "kill switch engaged"


def _result(mode, ok, started, detail, error=None) -> dict:
    return {"ok": ok, "mode": mode, "started_at": iso(started), "ended_at": iso(utc_now()),
            "detail": detail, "error": error}


def _run_command(step, runner, timeout) -> str | None:
    command = step["command"]
    try:
        done = runner(command, capture_output=True, text=True, timeout=timeout)
        return None if done.returncode == 0 else f"{' '.join(command)} exited {done.returncode}: {(done.stderr or '').strip()[:200]}"
    except subprocess.TimeoutExpired:
        return f"{' '.join(command)} timed out after {timeout}s"
    except OSError as exc:
        return f"cannot run docker: {exc}"


def _set_flags(step, flag_path) -> str | None:
    try:
        flag_adapter.set_resting(step["flags"], flag_path)
    except flag_adapter.FlagAdapterError as exc:
        return f"{step['text']}: {exc}"
    return None


def execute(proposal, *, dry_run=True, runner=None, health=container_health, actions_dir=None,
            timeout=COMMAND_TIMEOUT_S, flag_path=None) -> dict:
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
    step, why = registry.validate(proposal, health)
    if step is None:
        return blocked(f"proposal rejected: {why}")

    if dry_run:
        if step["flags"]:
            try:
                flag_adapter.check(step["flags"], flag_path)
            except flag_adapter.FlagAdapterError as exc:
                return blocked(f"the change cannot be made: {exc}")
        audit.record("dry_run", mode=mode, outcome="nothing changed", **base)
        would = f"Would run: {step['text']}" if step["command"] else f"Would {flag_adapter.exact_change(step['flags'])}"
        return _result(mode, True, started, f"dry run, nothing changed. {would}")

    already = [e for e in audit.read(actions_dir)
               if e.get("incident_id") == info["incident_id"] and e.get("event") == "execute_started"]
    if already:
        return blocked("this incident already had its one action; a second one is not allowed")
    if killswitch.is_engaged(actions_dir):  # checked again right before the command
        return blocked(f"{KILL_TEXT}; nothing was run")
    try:
        audit.record("execute_started", mode=mode, outcome=step["text"], **base)
    except OSError as exc:
        return _result("blocked", False, started, "not executed", f"audit log not writable, so nothing was run: {exc}")

    error = _run_command(step, runner, timeout) if step["command"] else _set_flags(step, flag_path)
    audit.record("execute_finished", mode=mode, outcome=error or "ok", **base)
    flags = ", ".join(step["flags"] or [])
    done = f"ran: {step['text']}" if step["command"] else f"{'tried to set' if error else 'set'} to {RESTING_VARIANT}: {flags}"
    return _result(mode, not error, started, done, error)
