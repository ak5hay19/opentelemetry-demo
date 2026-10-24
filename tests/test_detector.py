"""Offline tests for detector.detect: fake container state, fake metrics, fake clock, fake incident run."""
import re

import pytest

from detector import detect
from tests.test_actions import Clock, fake_health, row
from tools.result import ToolResult

NOW = "2026-10-24T05:00:00+00:00"
HEALTHY_ROWS = [row("cart", "running", "healthy"), row("valkey-cart", "running", "healthy")]
CAUSE_WORDS = ("because", "caused", "cause", "due to", "crash", "oom", "memory", "flag", "restart", "stopped", "killed",
               "dependency", "exit code", "likely", "root")


def services(*items):
    rows = [{"service": s, "error_rate_pct": e, "p95_ms": p} for s, e, p in items]
    return ToolResult("query_metrics", "data", ["q"], {}, items=rows, total_items=len(rows))


class Feed:
    """What the detector sees on each poll; the last entry repeats."""

    def __init__(self, containers=None, metrics=None):
        self.containers = containers or [HEALTHY_ROWS]
        self.metrics = metrics or [services(("cart", 0.0, 120.0), ("checkout", 0.5, 300.0))]
        self.polls = 0

    def health(self, include_stats=True):
        rows = self.containers[min(self.polls, len(self.containers) - 1)]
        return fake_health(*rows)(include_stats)

    def query(self, **kwargs):
        result = self.metrics[min(self.polls, len(self.metrics) - 1)]
        self.polls += 1
        return result


def detector(feed, cooldown_s=600, clock=None, log=None, **kwargs):
    clock = clock or Clock()
    return detect.Detector(health=feed.health, metrics=feed.query, clock=clock, now=lambda: __import__("datetime").datetime.fromisoformat(NOW),
                           log=(log if log is not None else []).append, cooldown_s=cooldown_s, **kwargs), clock


def poll_until_alert(det, polls=2):
    alerts = [det.check() for _ in range(polls)]
    return alerts


def test_fires_on_a_container_that_is_not_running_after_two_polls():
    feed = Feed(containers=[[row("cart", "running", "healthy"), row("valkey-cart", "exited")]])
    det, _ = detector(feed)
    first, second = poll_until_alert(det)
    assert first is None
    assert second["text"] == "ALERT: container valkey-cart is not running" and second["detected_at"] == NOW


def test_fires_on_error_ratio():
    feed = Feed(metrics=[services(("checkout", 31.0, 100.0), ("cart", 0.0, 100.0))])
    alert = poll_until_alert(detector(feed)[0])[1]
    assert alert["text"] == "ALERT: error ratio 0.31 on checkout above 0.05"


def test_fires_on_latency():
    feed = Feed(metrics=[services(("checkout", 0.0, 2500.0), ("cart", 0.0, 100.0))])
    alert = poll_until_alert(detector(feed)[0])[1]
    assert alert["text"] == "ALERT: p95 latency 2500 ms on checkout above 2000 ms"


def test_thresholds_are_respected():
    feed = Feed(metrics=[services(("checkout", 4.0, 1900.0))])
    assert poll_until_alert(detector(feed)[0], polls=4) == [None] * 4
    assert poll_until_alert(detector(feed, error_threshold=0.03)[0])[1]["text"].startswith("ALERT: error ratio 0.04 on checkout")
    assert "p95 latency 1900 ms" in poll_until_alert(detector(Feed(metrics=feed.metrics), p95_ms=1000)[0])[1]["text"]


def test_no_alert_on_healthy_data():
    det, _ = detector(Feed())
    assert poll_until_alert(det, polls=6) == [None] * 6


def test_a_condition_seen_once_is_not_enough():
    down = [row("valkey-cart", "exited")]
    feed = Feed(containers=[down, HEALTHY_ROWS, down, HEALTHY_ROWS, down])
    assert poll_until_alert(detector(feed)[0], polls=5) == [None] * 5


def test_a_different_condition_on_the_next_poll_does_not_count_as_confirmation():
    feed = Feed(metrics=[services(("checkout", 31.0, 100.0)), services(("cart", 31.0, 100.0))])
    assert poll_until_alert(detector(feed)[0]) == [None, None]


def test_one_alert_per_cooldown_period():
    feed = Feed(containers=[[row("valkey-cart", "exited")]])
    det, clock = detector(feed, cooldown_s=600)
    assert det.check() is None and det.check() is not None
    for _ in range(5):
        clock.sleep(100)
        assert det.check() is None
    clock.sleep(100)
    assert det.check() is not None


def test_stopped_load_generator_and_never_started_containers_are_not_alerts():
    feed = Feed(containers=[[row("load-generator", "exited"), row("frontend-tests", "created")]],
                metrics=[services(("load-generator", 90.0, 9000.0), ("cart", 0.0, 100.0))])
    assert poll_until_alert(detector(feed)[0], polls=4) == [None] * 4


def test_unavailable_signals_are_logged_and_never_treated_as_an_alert_or_as_health():
    log = []
    broken_h = lambda include_stats=True: ToolResult("container_health", "backend_error", ["q"], {}, error="docker down")
    no_data = ToolResult("query_metrics", "no_data", ["q"], {}, notes=["empty"])
    det = detect.Detector(health=broken_h, metrics=lambda **kw: no_data, clock=Clock(), log=log.append)
    assert [det.check(), det.check()] == [None, None]
    assert any("container state unavailable" in l for l in log) and any("metrics unavailable" in l for l in log)


def test_a_tool_that_raises_does_not_stop_the_detector():
    def boom(**kw):
        raise RuntimeError("nope")

    log = []
    det = detect.Detector(health=Feed().health, metrics=boom, clock=Clock(), log=log.append)
    assert det.check() is None and any("poll failed" in l for l in log)


def test_several_conditions_go_into_one_alert_named_but_capped():
    feed = Feed(containers=[[row("valkey-cart", "exited")]],
                metrics=[services(("cart", 60.0, 100.0), ("checkout", 31.0, 100.0), ("frontend", 20.0, 4000.0))])
    text = poll_until_alert(detector(feed)[0])[1]["text"]
    assert text == ("ALERT: container valkey-cart is not running; error ratio 0.60 on cart above 0.05; "
                    "error ratio 0.31 on checkout above 0.05; 2 more")


@pytest.mark.parametrize("feed", [
    Feed(containers=[[{**row("valkey-cart", "exited"), "exit_code": 137, "oom_killed": True}]]),
    Feed(metrics=[services(("checkout", 31.0, 100.0))]),
    Feed(metrics=[services(("checkout", 0.0, 9000.0))]),
    Feed(containers=[[row("cart", "restarting")]], metrics=[services(("cart", 80.0, 9000.0))]),
])
def test_alert_text_names_the_metric_and_service_but_no_cause(feed):
    text = poll_until_alert(detector(feed)[0])[1]["text"].lower()
    assert re.fullmatch(r"alert: (container [\w-]+ is not running|error ratio [\d.]+ on [\w-]+ above [\d.]+"
                        r"|p95 latency \d+ ms on [\w-]+ above \d+ ms)(; .*)?", text)
    assert not [w for w in CAUSE_WORDS if w in text]


# --- main -------------------------------------------------------------------------------------------

class Runs:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, argv, detected_at=None):
        self.calls.append((argv, detected_at))
        return self.code


def run_main(argv, feed, runs, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    code = detect.main(argv, run_fn=runs, sleep=sleeps.append, health=feed.health, metrics=feed.query, clock=Clock(),
                       now=lambda: __import__("datetime").datetime.fromisoformat(NOW), log=lambda *_: None)
    return code, sleeps


DOWN_FEED = lambda: Feed(containers=[[row("valkey-cart", "exited")]])


def test_main_once_runs_the_incident_with_the_alert_text_and_the_detection_time():
    runs = Runs()
    code, sleeps = run_main(["--once"], DOWN_FEED(), runs)
    assert code == 0 and runs.calls == [(["--alert", "ALERT: container valkey-cart is not running"], NOW)]
    assert sleeps == [30]


def test_main_passes_execute_only_when_the_detector_was_started_with_it():
    runs = Runs()
    run_main(["--once", "--execute"], DOWN_FEED(), runs)
    assert runs.calls[0][0][-1] == "--execute"
    runs = Runs()
    run_main(["--once"], DOWN_FEED(), runs)
    assert "--execute" not in runs.calls[0][0]


def test_main_once_with_healthy_data_polls_twice_and_runs_nothing():
    runs = Runs()
    code, sleeps = run_main(["--once", "--interval", "5"], Feed(), runs)
    assert code == 0 and runs.calls == [] and sleeps == [5]


def test_main_reports_a_failed_incident_run():
    assert run_main(["--once"], DOWN_FEED(), Runs(code=1))[0] == 1


def test_main_keeps_watching_and_respects_the_cooldown_until_interrupted():
    runs, sleeps = Runs(), []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 8:
            raise KeyboardInterrupt

    clock = Clock()
    feed = DOWN_FEED()
    detect.main(["--interval", "30", "--cooldown", "100"], run_fn=runs, sleep=lambda s: (clock.sleep(s), sleep(s)),
                health=feed.health, metrics=feed.query, clock=clock, log=lambda *_: None)
    assert len(runs.calls) == 2  # polls 2 (t=30) and 6 (t=150, cooldown 100s passed)


def test_main_rejects_bad_arguments():
    for argv in (["--interval", "0"], ["--error-threshold", "-1"], ["--p95-ms", "0"]):
        with pytest.raises(SystemExit) as exc:
            detect.main(argv)
        assert exc.value.code == 2


def test_detector_never_imports_dev_and_names_no_flag_service():
    from tests.test_isolation import REPO, imported_roots
    for path in (REPO / "detector").glob("*.py"):
        assert "dev" not in imported_roots(path), path
        assert "flagd" not in path.read_text(encoding="utf-8").lower(), path
