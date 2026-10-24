"""Watch the running shop and start an incident run when something crosses a line.

    python -m detector.detect [--once] [--interval 30] [--error-threshold 0.05] [--p95-ms 2000]
                              [--cooldown 600] [--execute]

Polls the read-only tools (container_health, query_metrics). A condition must hold on two consecutive polls
before it counts, and after an alert nothing else fires until the cooldown has passed. The alert text only
names the metric and the service that crossed the line, never a cause. The incident run (actions.run_incident)
happens in this process and asks for approval at this terminal; --execute lets an approved action really run.
--once polls twice (the confirmation pair), handles at most one alert and exits.
"""
import argparse
import sys
import time

from actions import run_incident
from actions.verify import IGNORED_SERVICES
from tools.containers import container_health
from tools.metrics import query_metrics
from tools.result import iso, utc_now

DEFAULT_INTERVAL_S = 30
DEFAULT_ERROR_THRESHOLD = 0.05  # errors / all calls, as a ratio
DEFAULT_P95_MS = 2000
DEFAULT_COOLDOWN_S = 600
CONFIRM_POLLS = 2
METRICS_WINDOW_MINUTES = 2  # the shortest window query_metrics accepts, so it reacts quickly
MAX_NAMED = 3  # conditions named in one alert text
# "created" means never started (e.g. an optional profile), which is not a failure.
DOWN_STATES = frozenset({"exited", "dead", "restarting", "paused"})

CONTAINER, ERRORS, LATENCY = "container", "errors", "latency"
ORDER = {CONTAINER: 0, ERRORS: 1, LATENCY: 2}


def describe(c: dict, error_threshold: float, p95_ms: float) -> str:
    if c["kind"] == CONTAINER:
        return f"container {c['subject']} is not running"
    if c["kind"] == ERRORS:
        return f"error ratio {c['value']:.2f} on {c['subject']} above {error_threshold:g}"
    return f"p95 latency {c['value']:.0f} ms on {c['subject']} above {p95_ms:g} ms"


def alert_text(conditions: list[dict], error_threshold: float, p95_ms: float) -> str:
    """Generic text: the metric and the service over the line. Never a cause."""
    pieces = [describe(c, error_threshold, p95_ms) for c in conditions[:MAX_NAMED]]
    if len(conditions) > MAX_NAMED:
        pieces.append(f"{len(conditions) - MAX_NAMED} more")
    return "ALERT: " + "; ".join(pieces)


class Detector:
    def __init__(self, *, error_threshold=DEFAULT_ERROR_THRESHOLD, p95_ms=DEFAULT_P95_MS, cooldown_s=DEFAULT_COOLDOWN_S,
                 health=container_health, metrics=query_metrics, clock=time.monotonic, now=utc_now, log=print):
        self.error_threshold, self.p95_ms, self.cooldown_s = error_threshold, p95_ms, cooldown_s
        self.health, self.metrics, self.clock, self.now, self.log = health, metrics, clock, now, log
        self.streaks: dict[tuple, int] = {}
        self.last_alert: float | None = None

    def _containers(self) -> list[dict]:
        result = self.health(include_stats=False)
        if result.state != "data":
            self.log(f"container state unavailable ({result.state}): not checked this poll")
            return []
        return [{"kind": CONTAINER, "subject": r["name"], "value": None} for r in result.items
                if r["status"] in DOWN_STATES and r["name"] not in IGNORED_SERVICES]

    def _services(self) -> list[dict]:
        result = self.metrics(window_minutes=METRICS_WINDOW_MINUTES, top_n=50)
        if result.state != "data":
            self.log(f"metrics unavailable ({result.state}): not checked this poll")
            return []
        found = []
        for r in result.items:
            if r["service"] in IGNORED_SERVICES:
                continue
            ratio = (r.get("error_rate_pct") or 0.0) / 100
            if ratio > self.error_threshold:
                found.append({"kind": ERRORS, "subject": r["service"], "value": ratio})
            if r.get("p95_ms") is not None and r["p95_ms"] > self.p95_ms:
                found.append({"kind": LATENCY, "subject": r["service"], "value": r["p95_ms"]})
        return found

    def poll(self) -> list[dict]:
        """Read the signals once. Returns the conditions that have now held for CONFIRM_POLLS polls in a row."""
        try:
            seen = self._containers() + self._services()
        except Exception as exc:  # a failing poll must not stop the watcher
            self.log(f"poll failed: {type(exc).__name__}: {exc}")
            seen = []
        self.streaks = {(c["kind"], c["subject"]): self.streaks.get((c["kind"], c["subject"]), 0) + 1 for c in seen}
        for c in seen:
            streak = self.streaks[(c["kind"], c["subject"])]
            if streak < CONFIRM_POLLS:
                self.log(f"watching: {describe(c, self.error_threshold, self.p95_ms)} ({streak}/{CONFIRM_POLLS})")
        confirmed = [c for c in seen if self.streaks[(c["kind"], c["subject"])] >= CONFIRM_POLLS]
        return sorted(confirmed, key=lambda c: (ORDER[c["kind"]], -(c["value"] or 0), c["subject"]))

    def check(self) -> dict | None:
        """Poll once; return an alert {"text", "detected_at", "conditions"} when one is due, else None."""
        confirmed = self.poll()
        if not confirmed:
            return None
        if self.last_alert is not None and self.clock() - self.last_alert < self.cooldown_s:
            return None
        self.last_alert = self.clock()
        return {"text": alert_text(confirmed, self.error_threshold, self.p95_ms), "detected_at": iso(self.now()),
                "conditions": confirmed}


def main(argv: list[str] | None = None, *, run_fn=run_incident.main, sleep=time.sleep, **dependencies) -> int:
    parser = argparse.ArgumentParser(description="Poll the shop and start an incident run when a service crosses a line")
    parser.add_argument("--once", action="store_true", help="poll twice (to confirm), handle at most one alert, exit")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, help="seconds between polls")
    parser.add_argument("--error-threshold", type=float, default=DEFAULT_ERROR_THRESHOLD,
                        help="alert when a service's error ratio (0 to 1) is above this")
    parser.add_argument("--p95-ms", type=float, default=DEFAULT_P95_MS, help="alert when a service's p95 latency is above this")
    parser.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN_S, help="seconds after an alert with no new alert")
    parser.add_argument("--execute", action="store_true", help="let an approved action really run (default is a dry run)")
    args = parser.parse_args(argv)
    if args.interval <= 0 or args.error_threshold <= 0 or args.p95_ms <= 0 or args.cooldown < 0:
        parser.error("--interval, --error-threshold and --p95-ms must be positive and --cooldown not negative")

    detector = Detector(error_threshold=args.error_threshold, p95_ms=args.p95_ms, cooldown_s=args.cooldown, **dependencies)
    detector.log(f"watching every {args.interval:g}s: error ratio > {args.error_threshold:g}, p95 > {args.p95_ms:g} ms, "
                 f"container not running; mode: {'EXECUTE (approved actions really run)' if args.execute else 'dry run'}")
    exit_code, polls = 0, 0
    try:
        while True:
            polls += 1
            alert = detector.check()
            if alert:
                detector.log(f"{alert['text']}  (detected {alert['detected_at']})")
                code = run_fn(["--alert", alert["text"], *(["--execute"] if args.execute else [])],
                              detected_at=alert["detected_at"])
                if code:
                    exit_code = 1
                    detector.log(f"the incident run failed (exit {code})")
                if args.once:
                    break
            if args.once and polls >= CONFIRM_POLLS:
                break
            sleep(args.interval)
    except KeyboardInterrupt:
        detector.log("stopped")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
