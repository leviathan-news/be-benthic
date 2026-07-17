#!/usr/bin/env python3
"""Evaluate reply-grounding attribution fixtures without publication or DB writes."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
_STATE_DIR = tempfile.TemporaryDirectory(prefix="benthic-grounding-eval-")
_STATE_ROOT = Path(_STATE_DIR.name)
sys.path.insert(0, str(ROOT))
os.environ["BENTHIC_BOT_TOKEN"] = "eval:stub-token-do-not-use"
os.environ["WALLET_PRIVATE_KEY"] = ""
os.environ["WALLET_KEY_FILE"] = str(ROOT / ".missing-wallet")
os.environ["ENABLE_REPLY_GROUNDING"] = "1"
os.environ["BENTHIC_DB"] = str(_STATE_ROOT / "agent.db")
os.environ["BENTHIC_LOG_FILE"] = str(_STATE_ROOT / "benthic.log")

from reply_grounding import (
    ComposedReply,
    EvidenceBundle,
    EvidenceItem,
    GroundedClaim,
)


def load_bot():
    """Load the real verifier while redirecting import-time SQLite writes to memory."""
    spec = importlib.util.spec_from_file_location(
        "benthic_bot_grounding_eval", ROOT / "benthic-bot.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    original_connect = sqlite3.connect
    production_db = (ROOT / "agent.db").resolve()
    isolated_db = Path(os.environ["BENTHIC_DB"]).resolve()

    def offline_connect(database, *args, **kwargs):
        """Keep import-time schema and knowledge initialization off production state."""
        try:
            requested = Path(database).expanduser().resolve()
        except (OSError, TypeError, ValueError):
            requested = None
        if requested in {production_db, isolated_db}:
            database = ":memory:"
        return original_connect(database, *args, **kwargs)

    sqlite3.connect = offline_connect
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sqlite3.connect = original_connect
    return module


@dataclass(frozen=True)
class Case:
    """One immutable evidence/composition pair with an expected live verdict."""

    name: str
    evidence: EvidenceBundle
    composition: ComposedReply
    expected_pass: bool


def item(evidence_id, kind, text, source_ref, *, timestamp=None, url=None):
    """Build one evaluator evidence item without touching external sources."""
    return EvidenceItem(
        evidence_id=evidence_id,
        kind=kind,
        text=text,
        source_ref=source_ref,
        timestamp=timestamp,
        url=url,
    )


def bundle(name, *items):
    """Build one bounded evaluator bundle with an explicit focal identifier."""
    return EvidenceBundle(
        trace_id=f"eval-{name}",
        chat_id=-1001234567890,
        message_id=1,
        direct=False,
        mode="grounded",
        focal_ids=("F1",) if any(row.evidence_id == "F1" for row in items) else (),
        items=tuple(items),
    )


focal = item(
    "F1",
    "focal_url",
    "Temporarily removing the 5 hour usage limit.",
    "x:2076365965915467978",
    timestamp="2026-07-12T17:59:57Z",
)
older = item(
    "B1",
    "background_url",
    "Theo described using CLIProxyAPI with a claudex alias.",
    "x:2076119366647894371",
    timestamp="2026-07-12T01:40:04Z",
)
undated_followup = item(
    "F1",
    "focal_url",
    "A follow-up post says the change is now generally available.",
    "x:2076365965915467999",
)
current = item(
    "M0", "current_message", "What do you think?", "telegram:-1001234567890:1"
)
opinion_current = item(
    "M0",
    "current_message",
    "The temporary limit removal trades more access for potentially higher "
    "demand. What do you think of that tradeoff?",
    "telegram:-1001234567890:1",
)
gap_request = item(
    "M0",
    "current_message",
    "Give me a current 4H entry and further catalysts for CASHCAT.",
    "telegram:-1001234567890:2",
)
wallet_sale = item(
    "B1",
    "background_url",
    "An early buyer sold 15.29M CASHCAT across 44 transactions.",
    "x:2074744919521104038",
    timestamp="2026-07-08T06:38:30Z",
)
burn_catalyst = item(
    "B2",
    "background_url",
    "The CASHCAT team announced a token burn for July 20.",
    "x:2074744919521104040",
    timestamp="2026-07-09T10:00:00Z",
)
exact_contract = "0x1111111111111111111111111111111111111111"
exact_token_request = item(
    "M0",
    "current_message",
    f"Find the best thesis for token contract {exact_contract} on eth.",
    "telegram:-1001234567890:3",
)
adjacent_chain_comment = item(
    "B1",
    "background_url",
    "Robinhood Chain works great for memes.",
    "x:2074695821896065360",
    url="https://x.com/vladtenev/status/2074695821896065360",
)
exact_token_thesis = item(
    "B1",
    "background_url",
    f"My thesis for contract {exact_contract} on eth is sustained fee growth.",
    "x:2075000000000000000",
    url="https://x.com/researcher/status/2075000000000000000",
)
draft_request = item(
    "M0",
    "current_message",
    "Write a concise two-sentence public draft about evidence standards. "
    "Do not discuss old provider failures.",
    "telegram:-1001234567890:4",
)
stale_runtime_failure = item(
    "T1",
    "runtime_receipt",
    "Three days ago, Codex made two consecutive source-checking failures.",
    "runtime:own_actions:11111111111111111111",
)


CASES = (
    Case(
        "incident_source_mismatch",
        bundle("incident", current, focal, older),
        ComposedReply(
            "reply",
            "The focal post recommends CLIProxyAPI and a claudex alias.",
            (GroundedClaim(
                "The focal post recommends CLIProxyAPI and a claudex alias.",
                ("B1",),
            ),),
        ),
        False,
    ),
    Case(
        "exact_focal_summary",
        bundle("focal", current, focal),
        ComposedReply(
            "reply",
            "The five-hour usage limit is temporarily removed.",
            (GroundedClaim(
                "The five-hour usage limit is temporarily removed.",
                ("F1",),
            ),),
        ),
        True,
    ),
    Case(
        "separately_attributed_older_source",
        bundle("older", current, focal, older),
        ComposedReply(
            "reply",
            "In an older post, Theo described CLIProxyAPI and a claudex alias.",
            (GroundedClaim(
                "An older Theo post described CLIProxyAPI and a claudex alias.",
                ("B1",),
            ),),
        ),
        False,
    ),
    Case(
        "unsupported_plural_generalization",
        bundle("plural", current, focal),
        ComposedReply(
            "reply",
            "OpenAI staff are telling users to replace Claude's model.",
            (GroundedClaim(
                "OpenAI staff are telling users to replace Claude's model.",
                ("F1",),
            ),),
        ),
        False,
    ),
    Case(
        "opinion_only_social_reply",
        bundle("opinion", opinion_current),
        ComposedReply(
            "reply",
            "That tradeoff looks sensible to me.",
            (),
        ),
        True,
    ),
    Case(
        "older_claim_missing_comparison_timestamp",
        bundle("missing-chronology-time", current, undated_followup, older),
        ComposedReply(
            "reply",
            "The CLIProxyAPI post is older than the follow-up post.",
            (GroundedClaim(
                "The CLIProxyAPI post is older than the follow-up post.",
                ("B1", "F1"),
            ),),
        ),
        False,
    ),
    Case(
        "older_claim_contradicted_by_timestamp_order",
        bundle("contradicted-chronology", current, focal, older),
        ComposedReply(
            "reply",
            "The limit-removal post is older than the CLIProxyAPI post.",
            (GroundedClaim(
                "The limit-removal post is older than the CLIProxyAPI post.",
                ("F1", "B1"),
            ),),
        ),
        False,
    ),
    Case(
        "scoped_evidence_gap",
        bundle("scoped-gap", gap_request, wallet_sale),
        ComposedReply(
            "reply",
            "A July 8 post reported that an early buyer sold 15.29M "
            "CASHCAT. I couldn't verify a current 4H entry or further "
            "catalysts from the sources I checked.",
            (GroundedClaim(
                "A July 8 post reported that an early buyer sold 15.29M "
                "CASHCAT.",
                ("B1",),
            ),),
        ),
        True,
    ),
    Case(
        "internal_protocol_leak",
        bundle("protocol-leak", gap_request, wallet_sale),
        ComposedReply(
            "reply",
            "I would not buy CASHCAT from this evidence. The supplied "
            "evidence does not establish its 4H setup or liquidity.",
            (),
        ),
        False,
    ),
    Case(
        "adjacent_chain_is_not_token_thesis",
        bundle("adjacent-thesis", exact_token_request, adjacent_chain_comment),
        ComposedReply(
            "reply",
            "The best thesis for that token is that Robinhood Chain works "
            "great for memes.",
            (GroundedClaim(
                "The best thesis for that token is that Robinhood Chain "
                "works great for memes.",
                ("B1",),
            ),),
        ),
        False,
    ),
    Case(
        "found_thesis_requires_public_url",
        bundle("missing-thesis-url", exact_token_request, exact_token_thesis),
        ComposedReply(
            "reply",
            "The best thesis I found is sustained fee growth for that token.",
            (GroundedClaim(
                "A thesis for the exact token is sustained fee growth.",
                ("B1",),
            ),),
        ),
        False,
    ),
    Case(
        "unscoped_world_absence",
        bundle("world-absence", gap_request, wallet_sale),
        ComposedReply(
            "reply",
            "There is no current 4H entry or further catalyst for CASHCAT.",
            (),
        ),
        False,
    ),
    Case(
        "scoped_gap_contradicted_by_evidence",
        bundle("false-scoped-gap", gap_request, wallet_sale, burn_catalyst),
        ComposedReply(
            "reply",
            "I couldn't verify any further CASHCAT catalyst from the sources "
            "I checked.",
            (),
        ),
        False,
    ),
    Case(
        "stale_runtime_refusal_is_not_task_fit",
        bundle("stale-task-refusal", draft_request, stale_runtime_failure),
        ComposedReply(
            "reply",
            "I can't write that draft because Codex made two consecutive "
            "source-checking failures three days ago.",
            (GroundedClaim(
                "Codex made two consecutive source-checking failures three "
                "days ago.",
                ("T1",),
            ),),
        ),
        False,
    ),
    Case(
        "aligned_current_task_draft",
        bundle("aligned-task-draft", draft_request, stale_runtime_failure),
        ComposedReply(
            "reply",
            "Show the source and state its limits. Let readers judge the "
            "claim from what the record actually supports.",
            (),
        ),
        True,
    ),
)


def evaluate_cases(bot):
    """Run every immutable case through the bot's real tools-disabled verifier."""
    matched = 0
    for case in CASES:
        verdict, receipt = bot._verify_grounded_reply(
            case.evidence,
            case.composition,
            permission_profile="benthic_bot",
        )
        actual = verdict.passed if verdict is not None else None
        ok = actual is case.expected_pass
        matched += int(ok)
        print(json.dumps({
            "case": case.name,
            "expected_pass": case.expected_pass,
            "actual_pass": actual,
            "matched": ok,
            "provider": receipt.provider if receipt else None,
            "model": receipt.model if receipt else None,
            "blocker": (
                None if verdict is not None else "verifier provider unavailable"
            ),
            "verification_mode": (
                "provider" if receipt else "deterministic" if verdict else None
            ),
            "verifier_reason": verdict.reason if verdict else None,
            "unsupported_claims": (
                list(verdict.unsupported_claims) if verdict else None
            ),
        }, sort_keys=True))
    print(json.dumps({"matched": matched, "total": len(CASES)}))
    return matched, len(CASES)


def main():
    """Exit successfully only when every live verifier verdict matches."""
    matched, total = evaluate_cases(load_bot())
    return 0 if matched == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
