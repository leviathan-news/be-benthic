"""Synthetic transcripts exercise ingress, extraction, grounding and publication gates."""

import importlib.util
import json
import logging
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "eval_reply_replays", ROOT / "scripts" / "eval_reply_replays.py"
)
replays = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replays
SPEC.loader.exec_module(replays)


@pytest.mark.parametrize("case", replays.load_cases(), ids=lambda case: case["name"])
def test_transcript_answers_current_task_from_exact_sources(case, tmp_path):
    result = replays.run_case(case, state_root=tmp_path)
    assert result["passed"], result
    assert result["ingress_calls"] >= 1
    assert result["transport_requests"]
    assert result["state_isolated"]
    assert result["external_publications"] == 0


@pytest.mark.parametrize("reply", [
    "I should have read the page. Please send the link again.",
    "The conversation recommends buying new servers.",
    "I cannot verify that reliably from the available sources.",
    "Aster proposes replaying complete user transcripts against useful answers.",
])
def test_usefulness_evaluator_rejects_empty_wrong_or_incomplete_answers(reply):
    case = next(case for case in replays.load_cases() if case["name"] == "split_url_caption")
    issues = replays.assess_answer(reply, case["turns"][-1]["expect"])
    assert issues


def test_usefulness_evaluator_accepts_supported_partial_answer():
    case = next(case for case in replays.load_cases() if case["name"] == "mixed_failure_first")
    reply = (
        "Aster proposes replaying complete user transcripts against useful answers. "
        "It recommends checking source identity after a correction. "
        "I couldn't read the audit page, so I can't compare its findings."
    )
    assert replays.assess_answer(reply, case["turns"][-1]["expect"]) == []


def test_streamed_fixture_has_no_static_visible_article():
    from reply_grounding import GroundingFailure
    transport = replays.FixtureTransport()
    fetcher = transport.fetcher()
    with pytest.raises(GroundingFailure, match="visible text"):
        fetcher.fetch(replays.STREAMED_URL)
    assert transport.requests == [replays.STREAMED_URL]


def test_default_replay_never_launches_browser_implicitly(tmp_path, monkeypatch):
    from browser_reader import BrowserSourceReader
    def unexpected(*args, **kwargs):
        pytest.fail("browser requires explicit replay opt-in")
    monkeypatch.setattr(BrowserSourceReader, "render", unexpected)
    result = replays.run_case(replays.streamed_case(), state_root=tmp_path)
    assert not result["passed"]
    assert result["turns"][0]["trace"]["failure_reason"]


@pytest.mark.skipif(
    os.environ.get("BENTHIC_REPLAY_BROWSER") != "1",
    reason="requires the actual isolated browser image; set BENTHIC_REPLAY_BROWSER=1",
)
def test_streamed_page_reaches_answer_through_actual_browser(tmp_path):
    result = replays.run_case(replays.streamed_case(), state_root=tmp_path, browser=True)
    assert result["passed"], result
    assert result["browser_enabled"]


def test_live_provider_requires_explicit_opt_in(tmp_path):
    with pytest.raises(ValueError, match="explicit"):
        replays.run_case(replays.load_cases()[0], state_root=tmp_path, live=True)


def test_evaluator_blocks_host_commands_even_with_nonempty_provider_reply(tmp_path):
    with replays.ReplayRuntime(tmp_path) as runtime:
        for name in (
            "tg_request", "_run_sandbox", "_run_benthic_build", "_run_pm2", "_run_operator_github",
            "resolve_telegram_fetcher", "resolve_twitter_fetcher", "YouTubeTranscriptFetcher",
        ):
            with pytest.raises(replays.ReplayBoundaryError):
                getattr(runtime.bot, name)("unexpected")
        assert runtime.bot.WALLET_KEY == ""
        assert runtime.bot._relay is None
        assert runtime.bot.DB_FILE.parent == tmp_path


def test_live_judge_contract_rejects_non_boolean_or_missing_checks():
    assert not replays.valid_judgment('{"task_completed":true}')
    assert not replays.valid_judgment(
        '{"task_completed":"true","source_identity":true,"supported_subset":true,"reason":"ok"}'
    )
    assert replays.valid_judgment(
        '{"task_completed":true,"source_identity":true,"supported_subset":true,"reason":"ok"}'
    )


def test_import_and_reload_do_not_change_callers_logging_or_state_paths(tmp_path, monkeypatch):
    outside = tmp_path / "unrelated-state.db"
    monkeypatch.setenv("BENTHIC_DB", str(outside))
    root_handlers = logging.getLogger().handlers[:]
    bot_logger = logging.getLogger("benthic-bot")
    logger_state = (bot_logger.handlers[:], bot_logger.propagate, bot_logger.level)
    with replays.ReplayRuntime(tmp_path / "isolated") as runtime:
        runtime.load()
        assert runtime.bot.DB_FILE == (tmp_path / "isolated" / "agent.db").resolve()
        assert not outside.exists()
    assert os.environ["BENTHIC_DB"] == str(outside)
    assert logging.getLogger().handlers == root_handlers
    assert (bot_logger.handlers, bot_logger.propagate, bot_logger.level) == logger_state


def _missing_claim_repair_replay(tmp_path, *, verifier_passes=True, invented_source=False,
                                 captured_repair=None):
    """Recover a real fetched source when the first draft contains only a promise."""
    case = replays.load_cases()[0]
    events = []
    with replays.ReplayRuntime(tmp_path) as runtime:
        bot = runtime.bot
        stub = replays.DeterministicProvider()
        stub.turn = case["turns"][0]
        now = int(replays.time.time())
        source = replays.message_from_event(case["history"][0], now=now)
        current = replays.message_from_event(case["turns"][0], now=now)
        bot.save_chat_message(source)

        def provider(prompt, validator, **kwargs):
            stage = replays.stage_name(prompt)
            events.append(stage)
            if stage == "composition":
                raw = json.dumps({
                    "decision": "reply", "reply": "Sorry. I'll read the source now.", "claims": [],
                })
            elif stage == "repair":
                # A valid repair needs permission to add the missing supported
                # claims, without granting permission to expand its evidence.
                assert "Do not add a new claim or source." not in prompt
                assert "original typed evidence" in prompt
                assert "You may add or replace claims only" in prompt
                assert "Distinguish each analytical inference from sourced fact in natural language" in prompt
                assert "When an objection identifies an unlabeled analytical inference" not in prompt
                focal = [row for row in replays.evidence_rows(prompt) if row["kind"] == "focal_url"]
                assert len(focal) == 1 and all(fact in focal[0]["text"] for fact in replays.FACTS)
                if captured_repair is not None:
                    raw = json.dumps(captured_repair["composition"])
                elif not invented_source:
                    return stub(prompt, validator, **kwargs)
                else:
                    raw = json.dumps({
                        "decision": "reply", "reply": replays.FACTS[0],
                        "claims": [{"claim": replays.FACTS[0], "evidence_ids": ["F999"]}],
                    })
                    assert not validator(raw)
                    return None
            elif stage == "verification":
                raw = json.dumps(captured_repair["verdict"] if captured_repair else {
                    "pass": verifier_passes,
                    "unsupported_claims": [] if verifier_passes else [replays.FACTS[0]],
                    "reason": "fixture final independent verdict",
                })
            else:
                return stub(prompt, validator, **kwargs)
            assert validator(raw)
            return replays.ProviderResult(raw, "codex", "gpt-5.6-terra" if kwargs.get("tier") else "gpt-5.6-sol",
                                          "medium" if kwargs.get("tier") else "xhigh", kwargs.get("tier"))

        bot._provider_chain.ask_validated = provider
        bot._process_one_message(current, [source], is_direct=True, is_private=False)
        reply = "\n".join(row["text"] for row in runtime.sent)
        with bot._db(row_factory=True) as conn:
            trace = dict(conn.execute("SELECT * FROM reply_grounding_traces").fetchone())
        assert runtime.transport.requests == [replays.SOURCE_URL]
        return reply, trace, events


@pytest.mark.parametrize("verifier_passes", [True, False])
def test_source_recovery_can_add_missing_supported_claim_but_requires_final_verification(tmp_path, verifier_passes):
    reply, trace, stages = _missing_claim_repair_replay(tmp_path, verifier_passes=verifier_passes)
    assert stages == ["engagement", "discovery", "composition", "repair", "verification"]
    if verifier_passes:
        assert all(fact in reply for fact in replays.FACTS)
        assert trace["disposition"] == "reply"
        assert trace["verifier_result"] == "pass"
    else:
        assert not any(fact in reply for fact in replays.FACTS)
        assert trace["failure_reason"] == "verification_failed"


def test_source_recovery_repair_cannot_introduce_a_new_source_id(tmp_path):
    reply, trace, stages = _missing_claim_repair_replay(tmp_path, invented_source=True)
    assert stages == ["engagement", "discovery", "composition", "repair"]
    assert not any(fact in reply for fact in replays.FACTS)
    assert trace["failure_reason"] == "repair_unavailable"


def test_source_recovery_repair_cannot_publish_captured_unlabelled_purposes(tmp_path):
    captured = json.loads((replays.FIXTURES / "repair-unlabelled-purposes.json").read_text())
    reply, trace, stages = _missing_claim_repair_replay(tmp_path, captured_repair=captured)
    assert stages[-2:] == ["repair", "verification"]
    assert trace["failure_reason"] == "verification_failed"
    assert "to test task continuity across the full exchange" not in reply
    assert "so the revised answer remains tied to the correct source" not in reply


def test_opt_in_diagnostics_preserve_candidates_and_verdicts_without_prompts(tmp_path):
    path = tmp_path / "private-diagnostics.jsonl"
    with replays.ReplayDiagnostics(path) as diagnostics:
        result = replays.run_case(replays.load_cases()[0], state_root=tmp_path / "state", diagnostics=diagnostics)
    assert result["passed"]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["stage"] for row in rows] == ["composition", "verification"]
    assert rows[0]["output"]["decision"] == "reply"
    assert rows[1]["output"]["pass"] is True
    assert all(set(row) == {"case", "message_id", "stage", "receipt", "output"} for row in rows)
    assert "Aster review of Harbor News" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "diagnostics" not in result


def test_diagnostics_refuse_custom_transcripts_before_import_or_provider_calls(tmp_path):
    case = json.loads(json.dumps(replays.load_cases()[0]))
    case["turns"][0]["text"] = "private non-fixture text"
    with replays.ReplayDiagnostics(tmp_path / "diagnostics.jsonl") as diagnostics:
        with pytest.raises(ValueError, match="built-in"):
            replays.run_case(case, state_root=tmp_path / "state", diagnostics=diagnostics)
    assert not (tmp_path / "state").exists()


def test_diagnostics_refuse_existing_paths_and_bound_model_output(tmp_path):
    path = tmp_path / "diagnostics.jsonl"
    path.write_text("keep")
    with pytest.raises(FileExistsError):
        replays.ReplayDiagnostics(path)
    assert path.read_text() == "keep"
    with replays.ReplayDiagnostics(tmp_path / "bounded.jsonl") as diagnostics:
        receipt = replays.ProviderResult("x" * 40000, "codex", "gpt-5.6-sol", "xhigh", None)
        diagnostics.record("split_url_caption", 101, "composition", receipt)
    row = json.loads((tmp_path / "bounded.jsonl").read_text())
    assert row["output"]["omitted"] == "stage_output_limit"
    assert "x" * 100 not in json.dumps(row)


def test_diagnostics_parse_runtime_json_omit_unparsed_text_and_redact_credentials(tmp_path):
    path = tmp_path / "diagnostics.jsonl"
    with replays.ReplayDiagnostics(path) as diagnostics:
        raw = '{"pass":false,"unsupported_claims":["unsupported"],"reason":"scope"}'
        diagnostics.record("split_url_caption", 101, "verification",
                           replays.ProviderResult(raw, "codex", "gpt-5.6-terra", "medium", "classification"))
        diagnostics.record("split_url_caption", 101, "verification",
                           replays.ProviderResult("```json\n" + raw + "\n```", "codex", "gpt-5.6-terra", "medium", "classification"))
        raw = json.dumps({"decision": "reply", "reply": "sk-" + "a" * 32, "claims": []})
        diagnostics.record("split_url_caption", 101, "composition",
                           replays.ProviderResult(raw, "codex", "gpt-5.6-sol", "xhigh", None))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["output"]["pass"] is False
    assert rows[1]["output"]["omitted"] == "unparsed_contract"
    assert rows[2]["output"]["omitted"] == "credential_shaped_output"
    assert "sk-" not in path.read_text()


def test_diagnostics_refuse_repo_paths_and_custom_case_cli(tmp_path):
    with pytest.raises(ValueError, match="outside the repository"):
        replays.ReplayDiagnostics(ROOT / "untracked-diagnostics.jsonl")
    with pytest.raises(SystemExit) as exc:
        replays.main(["--case-file", str(tmp_path / "cases.json"),
                      "--diagnostics-file", str(tmp_path / "diagnostics.jsonl")])
    assert exc.value.code == 2
    assert not (tmp_path / "diagnostics.jsonl").exists()
