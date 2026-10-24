"""The allowlist of actions the agent may propose, and the check every proposal must pass.

Two actions. restart_container: the command is always derived here from the container's current state,
never taken from the caller. disable_fault_flag: sets the catalogued fault toggles of a running component back
to off; the toggle names come from actions.remediations, never from the caller.
"""
import re

from actions.remediations import REMEDIATIONS, RESTING_VARIANT
from oncall import schema as S
from tools import sanitize
from tools.containers import container_health

# Restarting these destroys evidence or the monitoring stack, so they can never be a target.
# The flag service containers are refused through tools.sanitize.is_blocked_name instead of by name here.
DENY = frozenset({
    "jaeger", "prometheus", "opensearch", "otel-collector", "grafana", "kafka", "frontend-proxy",
})

CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
RESTART = "restart_container"
DISABLE_FLAG = "disable_fault_flag"

ACTIONS = {
    RESTART: {"enabled": True, "params": ("container",), "risk": "low", "label": "exact command"},
    DISABLE_FLAG: {"enabled": True, "params": ("component", "flags"), "risk": "low", "label": "exact change"},
}


def _denied(name) -> bool:
    return name in DENY or sanitize.is_blocked_name(name)


def command_for(container: str, status: str | None) -> list[str]:
    """`docker start` for a stopped container, `docker restart` for anything else."""
    verb = "start" if status in ("exited", "created") else "restart"
    return ["docker", verb, container]


def find_container(name: str, health=container_health, *, allow_denied=False) -> tuple[dict | None, str]:
    """The compose container whose name or compose service is `name`, or (None, why not)."""
    if not isinstance(name, str) or not CONTAINER_NAME.match(name):
        return None, f"{name!r} is not a valid container name"
    if not allow_denied and _denied(name):
        return None, f"{name} is on the deny list"
    result = health(include_stats=False)
    if result.state != "data":
        return None, f"container list unavailable ({result.state}: {result.error or ' '.join(result.notes)})"
    for row in result.items:
        if name in (row["name"], row.get("compose_service")):
            if not allow_denied and (_denied(row["name"]) or _denied(row.get("compose_service"))):
                return None, f"{row['name']} is on the deny list"
            return row, ""
    return None, f"no application container named {name} in the compose project"


def toggle_preview(component: str, flags) -> str:
    return f"set {component} fault toggles to {RESTING_VARIANT}: {', '.join(flags)} (nothing else changes)"


def target_container(proposal: dict) -> str:
    """The container whose state shows whether the action worked."""
    params = proposal["params"]
    return params.get("container") or params["component"]


def _validate_toggle(proposal: dict, params: dict) -> tuple[dict | None, str]:
    component, flags = params["component"], params["flags"]
    if not isinstance(component, str) or component not in REMEDIATIONS:
        return None, f"{component!r} has no catalogued fault toggles"
    if flags != list(REMEDIATIONS[component]):
        return None, f"flags {flags!r} are not the catalogued toggles of {component}"
    if proposal.get("preview") != toggle_preview(component, flags):
        return None, "preview does not match the change that would be made; the proposal is altered"
    return {"command": None, "flags": list(flags), "text": toggle_preview(component, flags)}, ""


def validate(proposal, health=container_health) -> tuple[dict | None, str]:
    """Re-check a proposal from scratch. Returns (step, "") when it may run, else (None, reason).

    A step is {"command": [...], "flags": None, "text": ...} for a restart and
    {"command": None, "flags": [...], "text": ...} for a flag change.
    """
    if not isinstance(proposal, dict) or not proposal.get(S.INCIDENT_ID):
        return None, "proposal has no incident_id"
    action_id = proposal.get("action_id")
    action = ACTIONS.get(action_id) if isinstance(action_id, str) else None
    if action is None or not action["enabled"]:
        return None, f"action {action_id!r} is not an enabled allowlisted action"
    params = proposal.get("params")
    if not isinstance(params, dict) or set(params) != set(action["params"]):
        return None, "proposal params do not match the action"
    if action_id == DISABLE_FLAG:
        return _validate_toggle(proposal, params)
    row, why = find_container(params["container"], health)
    if row is None or row["name"] != params["container"]:
        return None, why or f"{params['container']} is not a container name"
    command = command_for(row["name"], row["status"])
    if proposal.get("preview") != " ".join(command):
        return None, (f"preview {proposal.get('preview')!r} does not match the command that would run "
                      f"({' '.join(command)}); the proposal is altered or the container changed state")
    return {"command": command, "flags": None, "text": " ".join(command)}, ""


def plan(report, health=container_health) -> tuple[dict | None, str]:
    """(proposal, "") or (None, reason)."""
    if not isinstance(report, dict):
        return None, "no report"
    if report.get(S.STATUS) != S.STATUS_DIAGNOSED:
        why = report.get(S.ESCALATION_REASON) or "the report is not a confident diagnosis"
        return None, f"report status is {report.get(S.STATUS)!r}, not diagnosed: {why}"
    raw = report.get(S.COMPONENT)
    component = raw.strip().lower() if isinstance(raw, str) else raw
    row, why = find_container(component, health, allow_denied=True)
    if row is None:
        return None, f"component {raw!r} is not an allowed target: {why}"
    base = {S.INCIDENT_ID: report.get(S.INCIDENT_ID), S.COMPONENT: component, "reason": report.get(S.CAUSE)}
    if row["status"] != "running":
        if _denied(row["name"]) or _denied(row.get("compose_service")):
            return None, f"component {raw!r} is not running and is not an allowed restart target: it is on the deny list"
        command = command_for(row["name"], row["status"])
        return {**base, "action_id": RESTART, "params": {"container": row["name"]},
                "risk": ACTIONS[RESTART]["risk"], "preview": " ".join(command)}, ""
    if component in REMEDIATIONS:
        flags = list(REMEDIATIONS[component])
        return {**base, "action_id": DISABLE_FLAG, "params": {"component": component, "flags": flags},
                "risk": ACTIONS[DISABLE_FLAG]["risk"], "preview": toggle_preview(component, flags)}, ""
    return None, (f"component {raw!r} is not an allowed target: it is running and has no catalogued "
                  "fault toggles to turn off")


def propose(report, health=container_health) -> dict | None:
    return plan(report, health)[0]


def explain_no_proposal(report, health=container_health) -> str:
    proposal, why = plan(report, health)
    return why if proposal is None else "a proposal is available"
