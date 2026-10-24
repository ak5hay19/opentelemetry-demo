"""oncall.respond: investigate, then propose a fix for the run that was just created."""
import json

from oncall import respond
from tests.test_suggest import GOOD, SAMPLE, ScriptedLLM, choose


def fake_investigate(folder_name, status=0, write_candidate=True):
    def run(argv, llm=None, runs_dir=None):
        if folder_name:
            folder = runs_dir / folder_name
            folder.mkdir()
            if write_candidate:
                (folder / "candidate.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
        return status
    return run


def test_proposal_is_written_next_to_the_run_that_was_just_created(tmp_path, monkeypatch, capsys):
    old = tmp_path / "inc-old"
    old.mkdir()
    (old / "candidate.json").write_text(json.dumps({**SAMPLE, "component": "other"}), encoding="utf-8")
    monkeypatch.setattr(respond.investigate, "main", fake_investigate("inc-new"))
    assert respond.main([], llm=ScriptedLLM(choose(**GOOD)), runs_dir=tmp_path) == 0
    assert "PROPOSED FIX" in capsys.readouterr().out
    assert json.loads((tmp_path / "inc-new" / "proposal.json").read_text(encoding="utf-8"))["target"] == "valkey-cart"
    assert not (old / "proposal.json").exists()


def test_a_failed_investigation_stops_before_the_suggester(tmp_path, monkeypatch):
    monkeypatch.setattr(respond.investigate, "main", fake_investigate(None, status=2))
    llm = ScriptedLLM()
    assert respond.main([], llm=llm, runs_dir=tmp_path) == 2
    assert llm.calls == []


def test_no_new_run_with_a_candidate_is_reported_not_guessed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(respond.investigate, "main", fake_investigate("inc-new", write_candidate=False))
    assert respond.main([], llm=ScriptedLLM(), runs_dir=tmp_path) == 1
    assert "cannot tell which run" in capsys.readouterr().err
