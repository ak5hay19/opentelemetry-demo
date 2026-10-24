"""The allowlist of actions the agent may propose, and the check every proposal must pass.

Enabled actions are explicitly allowlisted here. Commands are always derived from validated action
parameters and current container state, never taken from caller-supplied command text.
"""
import re

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
FLUSH_CACHE = "flush_cache"
FLUSH_CACHE_CONTAINER = "valkey-cart"
FLUSH_CACHE_COMMAND = ["docker", "exec", FLUSH_CACHE_CONTAINER, "valkey-cli", "FLUSHDB", "ASYNC"]

ACTIONS = {
    RESTART: {"enabled": True, "params": ("container",), "risk": "low"},
    FLUSH_CACHE: {"enabled": True, "params": ("container", "scope"), "risk": "high"},
    # not implemented in v1; pending the organisers' Action API spec
    "toggle_flag": {"enabled": False},
}


def _denied(name) -> bool:
    return name in DENY or sanitize.is_blocked_name(name)


def command_for(container: str, status: str | None) -> list[str]:
    """`docker start` for a stopped container, `docker restart` for anything else."""
    verb = "start" if status in ("exited", "created") else "restart"
    return ["docker", verb, container]


def find_container(name: str, health=container_health) -> tuple[dict | None, str]:
    """The compose container whose name or compose service is `name`, or (None, why not)."""
    if not isinstance(name, str) or not CONTAINER_NAME.match(name):
        return None, f"{name!r} is not a valid container name"
    if _denied(name):
        return None, f"{name} is on the deny list"
    result = health(include_stats=False)
    if result.state != "data":
        return None, f"container list unavailable ({result.state}: {result.error or ' '.join(result.notes)})"
    for row in result.items:
        if name in (row["name"], row.get("compose_service")):
            if _denied(row["name"]) or _denied(row.get("compose_service")):
                return None, f"{row['name']} is on the deny list"
            return row, ""
    return None, f"no application container named {name} in the compose project"


def validate(proposal, health=container_health) -> tuple[list[str] | None, str]:
    """Re-check a proposal from scratch. Returns (command, "") when it may run, else (None, reason)."""
    if not isinstance(proposal, dict) or not proposal.get(S.INCIDENT_ID):
        return None, "proposal has no incident_id"
    action = ACTIONS.get(proposal.get("action_id"))
    if action is None or not action["enabled"]:
        return None, f"action {proposal.get('action_id')!r} is not an enabled allowlisted action"
    params = proposal.get("params")
    if not isinstance(params, dict) or set(params) != set(action["params"]):
        return None, "proposal params do not match the action"
    if proposal["action_id"] == FLUSH_CACHE:
        if params != {"container": FLUSH_CACHE_CONTAINER, "scope": "database"}:
            return None, "flush-cache is restricted to the valkey-cart database"
        row, why = find_container(FLUSH_CACHE_CONTAINER, health)
        if row is None or row["name"] != FLUSH_CACHE_CONTAINER:
            return None, why or f"{FLUSH_CACHE_CONTAINER} is not an allowed cache target"
        if row.get("status") != "running" or row.get("health") != "healthy":
            return None, f"{FLUSH_CACHE_CONTAINER} must be running and healthy"
        command = list(FLUSH_CACHE_COMMAND)
        if proposal.get("preview") != " ".join(command):
            return None, "flush-cache preview does not match the fixed allowlisted command"
        return command, ""
    row, why = find_container(params["container"], health)
    if row is None or row["name"] != params["container"]:
        return None, why or f"{params['container']} is not a container name"
    command = command_for(row["name"], row["status"])
    if proposal.get("preview") != " ".join(command):
        return None, (f"preview {proposal.get('preview')!r} does not match the command that would run "
                      f"({' '.join(command)}); the proposal is altered or the container changed state")
    return command, ""


def plan(report, health=container_health) -> tuple[dict | None, str]:
    """(proposal, "") or (None, reason)."""
    if not isinstance(report, dict):
        return None, "no report"
    if report.get(S.STATUS) != S.STATUS_DIAGNOSED:
        why = report.get(S.ESCALATION_REASON) or "the report is not a confident diagnosis"
        return None, f"report status is {report.get(S.STATUS)!r}, not diagnosed: {why}"
    component = report.get(S.COMPONENT)
    row, why = find_container(component, health)
    if row is None:
        return None, f"component {component!r} is not an allowed restart target: {why}"
    command = command_for(row["name"], row["status"])
    proposal = {
        S.INCIDENT_ID: report.get(S.INCIDENT_ID),
        S.COMPONENT: component,
        "action_id": RESTART,
        "params": {"container": row["name"]},
        "reason": report.get(S.CAUSE),
        "risk": ACTIONS[RESTART]["risk"],
        "preview": " ".join(command),
    }
    return proposal, ""


def propose(report, health=container_health) -> dict | None:
    return plan(report, health)[0]


def explain_no_proposal(report, health=container_health) -> str:
    proposal, why = plan(report, health)
    return why if proposal is None else "a proposal is available"
