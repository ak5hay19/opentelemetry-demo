"""Read-only recovery check after an action.

States:
  recovered                     target container running and healthy, and no service above the error threshold
  recovered_unverified_metrics  container healthy but metrics are no_data / backend_error: only partly verified
  not_recovered                 the last check saw a definite failure (container not healthy, or errors too high)
  timed_out                     the container's state could not be read before the deadline
no_data is never treated as proof of health.
"""
import time

from tools.containers import GOOD_HEALTH, container_health
from tools.metrics import query_metrics
from tools.result import iso, utc_now

ERROR_THRESHOLD_PCT = 5.0  # the alert error threshold: a service above this error rate has not recovered
METRICS_WINDOW_MINUTES = 2  # the shortest window query_metrics accepts
NO_ROLLBACK_NOTE = ("A restart has nothing to roll back, so if recovery is not confirmed the incident "
                    "must be escalated to a human.")

STATES = {
    "recovered": "recovered",
    "unverified": "recovered_unverified_metrics",
    "errors": "not_recovered",
    "unhealthy": "not_recovered",
    "unknown": "timed_out",
}


def _container_verdict(container, health) -> tuple[dict, str]:
    result = health(include_stats=False)
    if result.state != "data":
        return {"state": result.state, "detail": result.error or " ".join(result.notes)}, "unknown"
    row = next((r for r in result.items if r["name"] == container), None)
    if row is None:
        return {"state": "not_found", "detail": f"{container} is not in the container list"}, "unknown"
    info = {"state": "data", "status": row["status"], "health": row["health"], "restart_count": row["restart_count"]}
    return info, ("healthy" if row["status"] == "running" and row["health"] in GOOD_HEALTH else "unhealthy")


def _metrics_verdict(metrics, threshold) -> tuple[dict, str]:
    result = metrics(window_minutes=METRICS_WINDOW_MINUTES, top_n=50)
    if result.state != "data":
        return {"state": result.state, "detail": result.error or " ".join(result.notes)}, "unverified"
    rows = [(r.get("error_rate_pct") or 0.0, r["service"]) for r in result.items]
    worst_pct, worst = max(rows) if rows else (0.0, None)
    info = {"state": "data", "worst_service": worst, "worst_error_pct": worst_pct,
            "above_threshold": sorted(s for p, s in rows if p > threshold)}
    return info, ("errors" if info["above_threshold"] else "ok")


def check_recovery(proposal, *, timeout_s=120, poll_s=10, clock=time.monotonic, sleep=time.sleep,
                   health=container_health, metrics=query_metrics, threshold_pct=ERROR_THRESHOLD_PCT) -> dict:
    container = proposal["params"]["container"]
    deadline = clock() + timeout_s
    checks = []
    while True:
        container_info, c_verdict = _container_verdict(container, health)
        metrics_info, m_verdict = _metrics_verdict(metrics, threshold_pct)
        if c_verdict != "healthy":
            verdict = c_verdict
        elif m_verdict == "ok":
            verdict = "recovered"
        else:
            verdict = m_verdict  # "errors" or "unverified"
        checks.append({"at": iso(utc_now()), "container": container_info, "metrics": metrics_info, "verdict": verdict})
        if verdict == "recovered" or clock() + poll_s > deadline:
            break
        sleep(poll_s)

    state = STATES[checks[-1]["verdict"]]
    return {
        "state": state, "container": container, "checks": checks,
        "timeout_s": timeout_s, "threshold_pct": threshold_pct,
        "escalate": state not in ("recovered", "recovered_unverified_metrics"),
        "note": NO_ROLLBACK_NOTE,
    }
