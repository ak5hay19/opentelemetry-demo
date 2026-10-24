"""The approval gate: only an exact 'yes' on a still-valid proposal approves, and the decision is bound to that proposal."""
import json

import pytest

from oncall import approve, suggest
from tests.test_suggest import GOOD, SAMPLE, ScriptedLLM, choose


def proposed(**changes) -> dict:
    return {**suggest.suggest(SAMPLE, ScriptedLLM(choose(**GOOD))).to_dict(), **changes}


def answering(text):
    return lambda prompt: text


def refusing_to_be_asked(prompt):
    raise AssertionError("nobody should have been asked")


def test_yes_approves_and_binds_the_decision_to_the_proposal():
    p = proposed()
    d = approve.decide(p, answering("  YES "))
    assert d.status == approve.APPROVED
    assert d.proposal_sha256 == approve.proposal_hash(p)
    assert approve.is_approved_for(d.to_dict(), p)


@pytest.mark.parametrize("text", ["", "y", "ok", "yes please", "no", "yes!"])
def test_anything_but_an_exact_yes_rejects(text):
    assert approve.decide(proposed(), answering(text)).status == approve.REJECTED


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_no_input_rejects(error):
    def ask(prompt):
        raise error
    assert approve.decide(proposed(), ask).status == approve.REJECTED


def test_a_proposal_changed_after_approval_no_longer_matches():
    p = proposed()
    d = approve.decide(p, answering("yes")).to_dict()
    assert not approve.is_approved_for(d, {**p, "target": "other"})
    assert not approve.is_approved_for({**d, "status": approve.REJECTED}, p)


def test_an_escalation_asks_nobody():
    p = suggest.suggest({**SAMPLE, "stated_confidence": 0.1}, ScriptedLLM()).to_dict()
    assert approve.decide(p, refusing_to_be_asked).status == approve.NOT_APPLICABLE


@pytest.mark.parametrize("change", [
    {"action": "drop_database"},
    {"target": "frontend"},
    {"risk": ""},
    {"arguments": {"reduce_to_percent": 50}},
])
def test_an_edited_proposal_that_breaks_the_runbook_is_rejected_without_asking(change):
    d = approve.decide(proposed(**change), refusing_to_be_asked)
    assert d.status == approve.REJECTED
    assert "failed validation" in d.reason


def test_cli_saves_the_decision_next_to_the_proposal(tmp_path, capsys):
    run = tmp_path / "inc-1"
    run.mkdir()
    (run / "proposal.json").write_text(json.dumps(proposed()), encoding="utf-8")
    assert approve.main(["--run", "inc-1"], ask=answering("yes"), runs_dir=tmp_path) == 0
    assert "PROPOSED FIX" in capsys.readouterr().out
    assert json.loads((run / "decision.json").read_text(encoding="utf-8"))["status"] == approve.APPROVED


def test_cli_defaults_to_the_newest_run_with_a_proposal(tmp_path):
    for name in ("inc-old", "inc-new"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "proposal.json").write_text(json.dumps(proposed()), encoding="utf-8")
    approve.main([], ask=answering("no"), runs_dir=tmp_path)
    assert (tmp_path / "inc-new" / "decision.json").exists()
    assert not (tmp_path / "inc-old" / "decision.json").exists()


def test_cli_reports_an_unreadable_proposal(tmp_path, capsys):
    assert approve.main(["--proposal", str(tmp_path / "missing.json")], ask=refusing_to_be_asked, runs_dir=tmp_path) == 2
    assert "cannot read the proposal" in capsys.readouterr().err
