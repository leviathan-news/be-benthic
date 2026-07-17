"""Regression tests for the shared Benthic anti-slop prompt block.

The production modules own the module-level prompt constants, so these tests
import the same files the running agents use and render templates through the
shared prompt_loader.load_prompt() path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

from prompt_loader import load_prompt, _cache


ROOT = Path(__file__).parent.parent
MARKER = "NO AI SLOP"


def _load_hyphenated_module(module_name: str, filename: str) -> ModuleType:
    """Import a repo module whose filename is not a valid Python identifier."""
    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_bot_module() -> ModuleType:
    """Load benthic-bot.py with inert credentials so import-time config is local."""
    os.environ.setdefault("BENTHIC_BOT_TOKEN", "test:stub-token-do-not-use")
    os.environ.setdefault("WALLET_PRIVATE_KEY", "")
    os.environ.setdefault("WALLET_KEY_FILE", str(ROOT / ".missing-wallet-key-for-tests"))
    return _load_hyphenated_module("benthic_bot_no_ai_slop_test", "benthic-bot.py")


def _load_agent_module() -> ModuleType:
    """Load ln-agent.py without running its bottom-of-file agent loop."""
    os.environ.setdefault("WALLET_PRIVATE_KEY", "")
    return _load_hyphenated_module("ln_agent_no_ai_slop_test", "ln-agent.py")


def setup_function() -> None:
    """Clear the prompt cache so each test reads the current templates from disk."""
    _cache.clear()


def teardown_function() -> None:
    """Clear cached template text after each assertion group."""
    _cache.clear()


def test_shared_no_ai_slop_prompt_loads() -> None:
    """The shared prompt file must be loadable and contain the distinctive marker."""
    prompt = load_prompt("_shared/no_ai_slop")

    assert prompt.strip()
    assert MARKER in prompt


def test_creative_prompts_render_with_shared_no_slop_block() -> None:
    """Every creative-tier Benthic prose prompt receives the shared block."""
    bot = _load_bot_module()
    agent = _load_agent_module()

    rendered_prompts = {
        "bot/response_assembly": load_prompt(
            "bot/response_assembly",
            soul_block=bot.BENTHIC_SOUL,
            identity=bot.BENTHIC_IDENTITY,
            no_slop=bot.NO_AI_SLOP,
            security_block="SECURITY",
            topic_label="",
            activity="No recent activity.",
            own_actions="No recent actions.",
            positions="No open positions.",
            memory_notes="No notes.",
            knowledge="No knowledge block.",
            chat_history="No chat history.",
            conv_context="No conversation context.",
            message_block="CURRENT MESSAGE FROM test:\nhello",
            action="Reply normally.",
        ),
        "bot/grounded_response": load_prompt(
            "bot/grounded_response",
            soul_block=bot.BENTHIC_SOUL,
            identity=bot.BENTHIC_IDENTITY,
            no_slop=bot.NO_AI_SLOP,
            security_block="SECURITY",
            topic_label="",
            activity="",
            own_actions="",
            positions="",
            memory_notes="",
            knowledge="",
            conversation_evidence="[]",
            grounding_evidence="[]",
            action="Reply only if useful.",
        ),
        "bot/grounding_repair": load_prompt(
            "bot/grounding_repair",
            soul_block=bot.BENTHIC_SOUL,
            identity=bot.BENTHIC_IDENTITY,
            no_slop=bot.NO_AI_SLOP,
            security_block="SECURITY",
            evidence="[]",
            composition='{"decision":"reply","reply":"x","claims":[]}',
            objections='{"unsupported_claims":[],"reason":"x"}',
            action="Reply only if useful.",
        ),
        "agent/craft_comment": load_prompt(
            "agent/craft_comment",
            no_slop=agent.NO_AI_SLOP,
            safe_headline="Protocol raises $10M for restaking liquidity",
            tags_str="defi, restaking",
            url_line="",
        ),
        "agent/craft_reply": load_prompt(
            "agent/craft_reply",
            no_slop=agent.NO_AI_SLOP,
            safe_headline="Protocol raises $10M for restaking liquidity",
            our_comment_truncated="The cap table matters more than the slogan.",
            safe_author="alice",
            safe_reply="Why does that matter?",
        ),
        "agent/resolve_craft_headline_tldr": load_prompt(
            "agent/resolve_craft_headline_tldr",
            no_slop=agent.NO_AI_SLOP,
            url="https://example.com/story",
            safe_text="Example Telegram post about a protocol launch.",
        ),
    }

    for template_name, rendered in rendered_prompts.items():
        assert MARKER in rendered, f"{template_name} did not receive no-slop rules"


def test_classification_prompt_stays_free_of_no_slop_block() -> None:
    """Classification prompts must not receive creative voice guidance."""
    rendered_prompts = {
        "agent/evaluate_article_quality": load_prompt(
            "agent/evaluate_article_quality",
            safe_headline="Protocol raises $10M for restaking liquidity",
            tags_str="defi, restaking",
        ),
        "bot/grounding_research": load_prompt(
            "bot/grounding_research",
            max_sources=3,
            current_message="x",
            focal_evidence="[]",
            research_contract="GENERAL MODE",
        ),
        "bot/grounding_media": load_prompt(
            "bot/grounding_media",
            image_manifest="[]",
        ),
        "bot/grounding_verifier": load_prompt(
            "bot/grounding_verifier",
            evidence="[]",
            composition='{"decision":"reply","reply":"x","claims":[]}',
        ),
    }

    for template_name, rendered in rendered_prompts.items():
        assert MARKER not in rendered, f"{template_name} received creative voice rules"


def test_resolve_prompt_scopes_no_slop_to_tldr_not_headline() -> None:
    """The resolve prompt must keep headline guidance and delimiters untouched."""
    agent = _load_agent_module()
    rendered = load_prompt(
        "agent/resolve_craft_headline_tldr",
        no_slop=agent.NO_AI_SLOP,
        url="https://example.com/story",
        safe_text="Example Telegram post about a protocol launch.",
    )

    headline_rules = rendered.split("TL;DR RULES:", 1)[0].split("HEADLINE RULES:", 1)[1]

    assert MARKER not in headline_rules
    assert rendered.index(MARKER) > rendered.index("TL;DR RULES:")
    assert "===HEADLINE===\nYour headline here\n===TLDR===" in rendered
