"""The allowlisted remediation actions: the only things the agent may ever propose.

This is data, not behaviour. The fix suggester offers these to the model and rejects anything else.
The Action API (not built yet) should import RUNBOOK so that what can be proposed and what can be
executed are one list. Nothing here changes the system.

An action's `target_rule` limits its blast radius:
  "component"  the target must be exactly the component the diagnosis blames
  "fixed:NAME" the target is always NAME, whatever the diagnosis says
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RunbookAction:
    name: str
    description: str  # shown to the model
    target_rule: str
    reversible: bool
    parameters: dict = field(default_factory=dict)  # extra arguments: name -> {"type", "minimum", "maximum", "description"}


RUNBOOK: dict[str, RunbookAction] = {a.name: a for a in (
    RunbookAction(
        "restart_service",
        "Restart the service or container the diagnosis blames. Use it when that component is stopped, crashed, "
        "unhealthy or stuck, and bringing it back should clear the errors. Do not use it on a running, healthy component "
        "just because it shows errors: those errors usually come from a dependency.",
        target_rule="component",
        reversible=False,
    ),
    RunbookAction(
        "throttle_load",
        "Reduce the synthetic load sent to the application. Use it only when the diagnosis is overload or resource "
        "saturation and cutting traffic would let the system recover. It does not fix a broken component.",
        target_rule="fixed:load-generator",
        reversible=True,
        parameters={"reduce_to_percent": {
            "type": "integer", "minimum": 10, "maximum": 90,
            "description": "Keep this percentage of the current load (for example 50 halves it)."}},
    ),
)}

# Actions the event rules mention but that are switched off, with the reason. They are never offered to the model.
NOT_ENABLED = {
    "toggle_flag": "the agent may not read feature-flag state, so it cannot know which flag to name; "
                   "waiting for the organisers to say how a flag toggle may be proposed",
    "scale_service": "only available on Kubernetes; this setup runs on Docker Compose",
}
