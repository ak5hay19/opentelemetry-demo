"""bridge.run: respond (with approval) then the Action API, on the run that was just created."""
import json

from bridge import run
from tests.test_bridge_execute import STOPPED, FakeDocker, proposal
from tests.test_respond import fake_investigate
from tests.test_suggest import GOOD, SAMPLE, ScriptedLLM, choose

PROPOSE = dict(GOOD, target="cache-store")
DIAGNOSIS = {**SAMPLE, "component": "cache-store"}


def investigating(folder_name):
    def go(argv, llm=None, runs_dir=None):
        folder = runs_dir / folder_name
        folder.mkdir()
        (folder / "candidate.json").write_text(json.dumps(DIAGNOSIS), encoding="utf-8")
        return 0
    return go


def start(tmp_path, monkeypatch, argv, answer, docker):
    monkeypatch.setattr(run.respond.investigate, "main", investigating("inc-new"))
    return run.main(argv, llm=ScriptedLLM(choose(**PROPOSE)), runs_dir=tmp_path, ask=lambda p: answer,
                    health=STOPPED, command_runner=docker, actions_dir=tmp_path / "actions", verify_fn=lambda *a, **k: {
                        "state": "recovered", "detail": "ok", "escalate": False})


def result(tmp_path):
    return json.loads((tmp_path / "inc-new" / "execution.json").read_text(encoding="utf-8"))


def test_without_execute_it_is_only_a_dry_run(tmp_path, monkeypatch):
    docker = FakeDocker()
    assert start(tmp_path, monkeypatch, [], "yes", docker) == 0
    assert result(tmp_path)["outcome"] == "dry_run" and docker.commands == []


def test_with_execute_the_approved_command_runs_once(tmp_path, monkeypatch):
    docker = FakeDocker()
    assert start(tmp_path, monkeypatch, ["--execute"], "yes", docker) == 0
    assert docker.commands == [["docker", "start", "cache-store"]]
    assert result(tmp_path)["outcome"] == "executed_recovered"


def test_a_human_who_says_no_stops_everything(tmp_path, monkeypatch):
    docker = FakeDocker()
    start(tmp_path, monkeypatch, ["--execute"], "no", docker)
    assert docker.commands == [] and result(tmp_path)["outcome"] == "refused"
