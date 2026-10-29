"""The opt-in voice evaluation never substitutes a style score for verification."""
import json
from types import SimpleNamespace

import pytest

from providers import ProviderResult
from scripts import eval_reply_voice as voice
from scripts.eval_reply_replays import ReplayRuntime


@pytest.mark.parametrize('value', [None, [], {}, {'responsive': 1, 'natural_voice': True, 'reason': 'x'},
                                  {'responsive': True, 'natural_voice': True, 'reason': ''}])
def test_invalid_style_judgments_fail_closed(value):
    assert not voice.valid_judgment(json.dumps(value))


@pytest.mark.parametrize('verified', [False, True])
def test_voice_score_cannot_override_factual_verification(monkeypatch, verified):
    case = {'name': 'synthetic', 'message': 'A question', 'rubric': 'Be useful.'}
    judgment = json.dumps({'responsive': True, 'natural_voice': True, 'reason': 'Natural.'})
    def provider(prompt, validator, **kwargs):
        assert kwargs['tools'] == '__none__'
        assert validator(judgment)
        return ProviderResult(judgment, 'synthetic', 'fixture', 'none', None)
    result = SimpleNamespace(reply='An answer', verifier=SimpleNamespace(passed=verified),
                             failure_kind=None)
    bot = SimpleNamespace(
        _provider_chain=SimpleNamespace(ask_validated=provider),
        _run_grounded_pipeline=lambda turn: result,
        _finalize_generated_response=lambda reply, message, sender, **kw: reply,
    )
    monkeypatch.setattr(voice, 'make_turn', lambda *_: 'turn')
    row = voice.run_case(bot, provider, case)
    assert row['passed'] is verified
    assert bot._provider_chain.ask_validated is provider
    assert row['external_publications'] == 0


def test_live_voice_run_requires_explicit_opt_in():
    with pytest.raises(SystemExit) as exc:
        voice.main([])
    assert exc.value.code == 2


def test_conversation_uses_actual_final_replies_and_keeps_ambient_turns(monkeypatch):
    observed = []
    def run_case(bot, provider, case):
        observed.append(case)
        return {'case': case['name'], 'passed': True,
                'reply': 'Published reply' if len(observed) == 1 else '',
                'decision': 'reply' if len(observed) == 1 else 'skip'}
    monkeypatch.setattr(voice, 'run_case', run_case)
    conversation = {'name': 'continuity', 'turns': [
        {'message': 'An initial question', 'rubric': 'Answer it.'},
        {'message': 'Thanks, agreed.', 'direct': False, 'expected_decision': 'skip',
         'rubric': 'Do not repeat yourself.'},
        {'message': 'One more question', 'rubric': 'Answer the follow-up.'},
    ]}
    rows = voice.run_conversation(None, None, conversation)
    assert len(rows) == 3
    assert observed[0]['history'] == []
    assert observed[1]['history'] == [
        {'speaker': 'human', 'text': 'An initial question'},
        {'speaker': 'benthic', 'text': 'Published reply'},
    ]
    assert observed[1]['direct'] is False
    assert observed[2]['history'][-1] == {'speaker': 'human', 'text': 'Thanks, agreed.'}
    assert len(observed[2]['history']) == 3


def test_seeded_banter_records_draft_and_final_verdict_without_inventing_claims(tmp_path):
    with ReplayRuntime(tmp_path) as runtime:
        def provider(prompt, validator, **kwargs):
            assert kwargs['tools'] == '__none__'
            if 'fail-closed factual-support verifier' in prompt:
                raw = json.dumps({'pass': True, 'unsupported_claims': [], 'reason': ''})
            else:
                raw = json.dumps({'responsive': True, 'natural_voice': True, 'reason': 'A contextual joke.'})
            assert validator(raw)
            return ProviderResult(raw, 'synthetic', 'fixture', 'none', None)
        row = voice.run_case(runtime.bot, provider, {
            'name': 'metaphor', 'message': 'Benthic, you have become a pitch deck.',
            'seed_reply': 'And yet you stayed for the Q&A.', 'seed_claims': [],
            'preserve_seed': True, 'rubric': 'Keep the metaphor.',
        })
        assert row['passed']
        assert row['reply'] == 'And yet you stayed for the Q&A.'
        assert row['stages'][0]['output']['claims'] == []
        assert row['stages'][1]['output']['pass'] is True


def test_preserve_seed_cannot_pass_after_repair_erases_it(tmp_path):
    with ReplayRuntime(tmp_path) as runtime:
        calls = 0
        def provider(prompt, validator, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                value = {'pass': False, 'unsupported_claims': ['Q&A'], 'reason': 'literal claim'}
            elif calls == 2:
                value = {'decision': 'reply', 'reply': 'Fair. I will do better.', 'claims': []}
            elif calls == 3:
                value = {'pass': True, 'unsupported_claims': [], 'reason': ''}
            else:
                value = {'responsive': True, 'natural_voice': True, 'reason': 'Generic style approval.'}
            raw = json.dumps(value)
            assert validator(raw)
            return ProviderResult(raw, 'synthetic', 'fixture', 'none', None)
        row = voice.run_case(runtime.bot, provider, {
            'name': 'metaphor', 'message': 'Benthic, you have become a pitch deck.',
            'seed_reply': 'And yet you stayed for the Q&A.', 'seed_claims': [],
            'preserve_seed': True, 'rubric': 'Keep the metaphor.',
        })
        assert row['verified']
        assert not row['passed']


def test_ambient_skip_passes_only_when_no_reply_was_emitted(tmp_path):
    with ReplayRuntime(tmp_path) as runtime:
        def provider(prompt, validator, **kwargs):
            assert 'did not address you directly' in prompt
            assert '@benthic: We can measure it after the launch.' in prompt
            raw = json.dumps({'decision': 'skip', 'reply': '', 'claims': []})
            assert validator(raw)
            return ProviderResult(raw, 'synthetic', 'fixture', 'none', None)
        row = voice.run_case(runtime.bot, provider, {
            'name': 'ack', 'message': 'Agreed.', 'direct': False,
            'expected_decision': 'skip', 'rubric': 'No repeated advice.',
            'history': [{'speaker': 'benthic', 'text': 'We can measure it after the launch.'}],
        })
        assert row['passed']
        assert row['decision'] == 'skip'
        assert row['reply'] == ''
