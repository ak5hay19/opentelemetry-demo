"""Throttling the load generator through the bridge. A fake Locust answers; nothing real is contacted."""
from types import SimpleNamespace

import requests

from actions import audit, killswitch
from bridge import execute, throttle
from tests.test_bridge_execute import STOPPED, make_run, proposal

URL = "http://localhost:8080/loadgen"
RECOVERED = {"state": "recovered", "detail": "ok", "escalate": False}


class FakeLocust:
    def __init__(self, users=10, state="running", fail_post=False):
        self.users, self.state, self.fail_post, self.posts = users, state, fail_post, []

    def get(self, url, timeout=None):
        assert url == f"{URL}/stats/requests"
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"user_count": self.users, "state": self.state})

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, data))
        return SimpleNamespace(ok=not self.fail_post, status_code=500 if self.fail_post else 200)


def throttle_proposal(percent=50):
    return proposal(action="throttle_load", target="load-generator", arguments={"reduce_to_percent": percent})


def go(folder, tmp_path, locust, verify_fn=None, **kwargs):
    return execute.run(folder, health=STOPPED, actions_dir=tmp_path / "actions", http_get=locust.get,
                       http_post=locust.post, loadgen=URL, verify_fn=verify_fn or (lambda target, **k: RECOVERED), **kwargs)


def test_plan_halves_the_users_and_shows_the_exact_request():
    action, why = throttle.plan(throttle_proposal(50), "inc-1", url=URL, http_get=FakeLocust(users=10).get)
    assert action["params"] == {"reduce_to_percent": 50, "from_users": 10, "to_users": 5}
    assert action["preview"] == f"POST {URL}/swarm user_count=5 spawn_rate=10"


def test_dry_run_changes_nothing(tmp_path):
    locust = FakeLocust()
    out = go(make_run(tmp_path, throttle_proposal()), tmp_path, locust)
    assert out["outcome"] == "dry_run" and locust.posts == []


def test_execute_sets_the_user_count_once_and_checks_recovery(tmp_path):
    locust = FakeLocust(users=10)
    out = go(make_run(tmp_path, throttle_proposal()), tmp_path, locust, execute=True)
    assert locust.posts == [(f"{URL}/swarm", {"user_count": 5, "spawn_rate": 10})]
    assert out["outcome"] == "executed_recovered"
    assert [e["event"] for e in audit.read(tmp_path / "actions")] == ["execute_started", "execute_finished"]


def test_recovery_is_checked_on_the_shop_not_the_generator(tmp_path):
    seen = []
    go(make_run(tmp_path, throttle_proposal()), tmp_path, FakeLocust(), execute=True,
       verify_fn=lambda target, **k: seen.append(target) or RECOVERED)
    assert seen == [{"params": {"container": "load-generator"}}]


def test_a_failed_change_is_reported(tmp_path):
    out = go(make_run(tmp_path, throttle_proposal()), tmp_path, FakeLocust(fail_post=True), execute=True)
    assert out["outcome"] == "failed" and "HTTP 500" in out["reason"] and out["verification"] is None


def test_nothing_changes_without_an_approval(tmp_path):
    locust = FakeLocust()
    out = go(make_run(tmp_path, throttle_proposal(), answer="no"), tmp_path, locust, execute=True)
    assert out["outcome"] == "refused" and locust.posts == []


def test_the_kill_switch_blocks_the_throttle(tmp_path):
    killswitch.engage(tmp_path / "actions")
    locust = FakeLocust()
    out = go(make_run(tmp_path, throttle_proposal()), tmp_path, locust, execute=True)
    assert out["outcome"] == "blocked" and locust.posts == []


def test_only_one_action_per_incident(tmp_path):
    locust = FakeLocust()
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
    go(make_run(tmp_path / "first", throttle_proposal()), tmp_path, locust, execute=True)
    out = go(make_run(tmp_path / "second", throttle_proposal()), tmp_path, locust, execute=True)
    assert out["outcome"] == "blocked" and "one action" in out["reason"] and len(locust.posts) == 1


def test_refuses_when_there_is_nothing_to_throttle(tmp_path):
    for name, locust, expect in (("stopped", FakeLocust(state="stopped"), "not running"),
                                 ("single", FakeLocust(users=1), "changes nothing")):
        (tmp_path / name).mkdir()
        out = go(make_run(tmp_path / name, throttle_proposal()), tmp_path / name, locust, execute=True)
        assert out["outcome"] == "refused" and expect in out["reason"] and locust.posts == []


def test_an_unreachable_load_generator_is_a_refusal_not_a_crash(tmp_path):
    def down(url, timeout=None):
        raise requests.exceptions.ConnectionError("refused")
    locust = FakeLocust()
    out = execute.run(make_run(tmp_path, throttle_proposal()), health=STOPPED, actions_dir=tmp_path / "a",
                      http_get=down, http_post=locust.post, loadgen=URL, execute=True)
    assert out["outcome"] == "refused" and "cannot read the load generator" in out["reason"]


def test_only_loopback_addresses_are_ever_contacted(tmp_path):
    locust = FakeLocust()
    out = execute.run(make_run(tmp_path, throttle_proposal()), health=STOPPED, actions_dir=tmp_path / "a",
                      http_get=locust.get, http_post=locust.post, loadgen="http://10.1.2.3:8089", execute=True)
    assert out["outcome"] == "refused" and "local http address" in out["reason"] and locust.posts == []


def test_restore_puts_the_users_back(tmp_path):
    locust = FakeLocust(users=10)
    folder = make_run(tmp_path, throttle_proposal())
    go(folder, tmp_path, locust, execute=True)
    code = throttle.main(["--restore", "--run-dir", str(folder), "--execute"], url=URL, http_post=locust.post,
                         actions_dir=tmp_path / "actions")
    assert code == 0 and locust.posts[-1] == (f"{URL}/swarm", {"user_count": 10, "spawn_rate": 10})
    assert [e["event"] for e in audit.read(tmp_path / "actions")][-2:] == ["restore_started", "restore_finished"]


def test_restore_is_a_dry_run_unless_asked(tmp_path):
    locust = FakeLocust()
    folder = make_run(tmp_path, throttle_proposal())
    go(folder, tmp_path, locust, execute=True)
    sent = len(locust.posts)
    throttle.main(["--restore", "--run-dir", str(folder)], url=URL, http_post=locust.post, actions_dir=tmp_path / "actions")
    assert len(locust.posts) == sent


def test_the_throttle_amount_is_bounded():
    for bad in (5, 95, 0, True, "50"):
        assert throttle.plan(throttle_proposal(bad), "i", url=URL, http_get=FakeLocust().get)[0] is None
