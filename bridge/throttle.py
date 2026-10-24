"""Throttle the load generator: lower the number of simulated users it runs (Locust's web interface).

The Action API (actions/) can only restart containers, so this module adds the throttle next to it without changing it.
It keeps the same safety contract and reuses the Action API's own kill switch and audit log:
  - dry run by default; nothing is changed unless execute=True
  - the kill switch is checked first and again right before the change
  - one action per incident, every step written to the audit log
  - only a loopback address is ever contacted, so this cannot reach another machine or the venue network
  - throttle_load is reversible: restore() puts the user count back to what it was

    python -m bridge.throttle --restore --run-dir reports/runs/<id> [--execute]

Needs the load generator's web interface, by default http://localhost:8080/loadgen (through the demo's front proxy).
Set LOADGEN_URL in .env.incident to change it.
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

from actions import audit, killswitch
from tools.endpoints import load_settings
from tools.result import iso, utc_now

THROTTLE = "throttle_load"
DEFAULT_LOADGEN_URL = "http://localhost:8080/loadgen"
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
HTTP_TIMEOUT = 10
MIN_PERCENT, MAX_PERCENT = 10, 90


def loadgen_url(settings: dict | None = None) -> str:
    settings = load_settings() if settings is None else settings
    return settings.get("LOADGEN_URL", DEFAULT_LOADGEN_URL).rstrip("/")


def _loopback_problem(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK:
        return f"{url} is not a local http address; the load generator is only ever throttled on this machine"
    return None


def read_state(url: str, http_get=requests.get) -> tuple[dict | None, str]:
    """The load generator's current state: ({"users": n, "state": "running"}, "") or (None, why not)."""
    problem = _loopback_problem(url)
    if problem:
        return None, problem
    try:
        response = http_get(f"{url}/stats/requests", timeout=HTTP_TIMEOUT)
        if not response.ok:
            return None, f"load generator answered HTTP {response.status_code}"
        body = response.json()
        return {"users": int(body["user_count"]), "state": str(body.get("state", ""))}, ""
    except (requests.exceptions.RequestException, ValueError, KeyError, TypeError) as exc:
        return None, f"cannot read the load generator: {type(exc).__name__}: {str(exc)[:100]}"


def plan(proposal: dict, incident_id: str, *, url: str | None = None, http_get=requests.get) -> tuple[dict | None, str]:
    """The throttle as an action dict (the Action API's shape), or (None, why it cannot be run)."""
    percent = (proposal.get("arguments") or {}).get("reduce_to_percent")
    if isinstance(percent, bool) or not isinstance(percent, int) or not MIN_PERCENT <= percent <= MAX_PERCENT:
        return None, f"reduce_to_percent must be an integer from {MIN_PERCENT} to {MAX_PERCENT}"
    url = url or loadgen_url()
    state, why = read_state(url, http_get)
    if state is None:
        return None, why
    if state["state"] != "running":
        return None, f"the load generator is {state['state'] or 'in an unknown state'}, not running, so there is nothing to throttle"
    users = state["users"]
    target = max(1, round(users * percent / 100))
    if target >= users:
        return None, f"the load generator already runs only {users} user(s); throttling to {percent}% changes nothing"
    return {
        "incident_id": incident_id, "component": "load-generator", "action_id": THROTTLE,
        "params": {"reduce_to_percent": percent, "from_users": users, "to_users": target},
        "reason": proposal.get("reason"), "risk": "low",
        "preview": f"POST {url}/swarm user_count={target} spawn_rate={users}",
    }, ""


def _result(mode: str, ok: bool, started, detail: str, error: str | None = None) -> dict:
    return {"ok": ok, "mode": mode, "started_at": iso(started), "ended_at": iso(utc_now()), "detail": detail, "error": error}


def _set_users(url: str, users: int, spawn_rate: int, http_post) -> str | None:
    """Ask the load generator to run `users` users. Returns an error text, or None on success."""
    try:
        response = http_post(f"{url}/swarm", data={"user_count": users, "spawn_rate": spawn_rate}, timeout=HTTP_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return f"cannot reach the load generator: {type(exc).__name__}: {str(exc)[:100]}"
    return None if response.ok else f"the load generator answered HTTP {response.status_code}"


def execute(action: dict, *, dry_run: bool = True, url: str | None = None, http_post=requests.post,
            actions_dir=None, event: str = "execute", users: int | None = None) -> dict:
    """Run (or dry-run) a throttle action. `event` and `users` let restore() reuse the same checks."""
    started = utc_now()
    url = url or loadgen_url()
    params = action["params"]
    target = params["to_users"] if users is None else users
    base = {"incident_id": action["incident_id"], "action_id": action["action_id"], "params": params, "directory": actions_dir}
    mode = "dry_run" if dry_run else "executed"

    def blocked(reason: str) -> dict:
        audit.record("blocked", mode="blocked", outcome=reason, **base)
        return _result("blocked", False, started, "not executed", reason)

    if killswitch.is_engaged(actions_dir):
        return blocked("kill switch engaged; nothing was changed")
    problem = _loopback_problem(url)
    if problem:
        return blocked(problem)
    if dry_run:
        audit.record("dry_run", mode=mode, outcome="nothing changed", **base)
        return _result(mode, True, started, f"dry run, nothing changed. Would set the load generator to {target} users")

    if event == "execute":
        already = [e for e in audit.read(actions_dir)
                   if e.get("incident_id") == action["incident_id"] and e.get("event") == "execute_started"]
        if already:
            return blocked("this incident already had its one action; a second one is not allowed")
    if killswitch.is_engaged(actions_dir):  # checked again right before the change
        return blocked("kill switch engaged; nothing was changed")
    try:
        audit.record(f"{event}_started", mode=mode, outcome=f"set users to {target}", **base)
    except OSError as exc:
        return _result("blocked", False, started, "not executed", f"audit log not writable, so nothing was changed: {exc}")

    error = _set_users(url, target, max(1, params["from_users"]), http_post)
    audit.record(f"{event}_finished", mode=mode, outcome=error or "ok", **base)
    if error:
        return _result(mode, False, started, f"tried to set the load generator to {target} users", error)
    return _result(mode, True, started, f"set the load generator to {target} users (was {params['from_users']})")


def restore(action: dict, **kwargs) -> dict:
    """Put the user count back to what it was before the throttle."""
    return execute(action, event="restore", users=action["params"]["from_users"], **kwargs)


def main(argv: list[str] | None = None, **dependencies) -> int:
    parser = argparse.ArgumentParser(description="Undo a throttle: set the load generator back to its earlier user count")
    parser.add_argument("--restore", action="store_true", required=True)
    parser.add_argument("--run-dir", required=True, help="a run folder whose execution.json holds a throttle")
    parser.add_argument("--execute", action="store_true", help="really change it; without this only a dry run")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        saved = json.loads((Path(args.run_dir) / "execution.json").read_text(encoding="utf-8"))
        action = saved["action_proposal"]
        if action["action_id"] != THROTTLE:
            raise KeyError("not a throttle")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"no throttle to restore in {args.run_dir}: {exc}", file=sys.stderr)
        return 2
    result = restore(action, dry_run=not args.execute, **dependencies)
    print(f"{'RESTORED' if result['mode'] == 'executed' and result['ok'] else result['mode'].upper()}: "
          f"{result['detail']}" + (f" ({result['error']})" if result["error"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
