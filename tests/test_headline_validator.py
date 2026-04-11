"""Tests for validate-headline.sh — LN editorial rule validator.

Each test shells out directly to the bash script and checks the exit code.
Exit code semantics:
  0 — all checks passed (warnings are allowed)
  1 — one or more hard-fail checks triggered
  2 — no headline argument was provided
"""
import subprocess
from pathlib import Path

# Resolve the validator path relative to this file's location so the tests
# work regardless of the current working directory.
VALIDATOR = Path(__file__).parent.parent / "skills/leviathan-headlines/scripts/validate-headline.sh"


def _run_validator(headline: str) -> int:
    """Run validate-headline.sh with the given headline and return its exit code.

    Captures stdout/stderr to suppress terminal output during test runs.
    The 10-second timeout prevents a runaway shell from blocking the suite.
    """
    result = subprocess.run(
        [str(VALIDATOR), headline],
        capture_output=True, text=True, timeout=10
    )
    return result.returncode


def test_good_headline_passes():
    """A well-formed headline with no hard errors should exit 0.

    The headline triggers a sentence-case warning for 'Chain' (not in the
    known-proper-nouns list), but warnings do not change the exit code.
    """
    headline = "Hyperliquid tops Ethereum, Solana, Bitcoin, and BNB Chain combined in 24-hour fees with just 11 employees"
    assert _run_validator(headline) == 0


def test_too_short_fails():
    """A headline below the 75-character minimum should exit 1."""
    assert _run_validator("Short headline here") == 1


def test_too_long_fails():
    """A headline above the 150-character maximum should exit 1.

    "A" + " word" * 40 = 201 characters, well over the limit.
    Note: the string also starts with the article "A", which adds a second
    error — the script still exits 1 regardless of how many errors fired.
    """
    long = "A" + " word" * 40
    assert _run_validator(long) == 1


def test_trailing_period_fails():
    """A headline ending with a period should exit 1."""
    headline = "Ethereum gas fees hit all-time low as Dencun upgrade reduces blob costs by ninety percent across all rollups."
    assert _run_validator(headline) == 1


def test_leading_article_fails():
    """A headline that starts with a leading article ('The') should exit 1."""
    headline = "The Ethereum Foundation announces major restructuring of its grant program affecting over 200 active projects"
    assert _run_validator(headline) == 1


def test_first_person_fails():
    """A headline containing first-person pronouns ('We') should exit 1."""
    headline = "We believe Ethereum will outperform Bitcoin in the next quarter based on current on-chain metrics and analysis"
    assert _run_validator(headline) == 1


def test_at_symbol_fails():
    """A headline containing an '@' symbol should exit 1 (Telegram incompatible)."""
    headline = "@VitalikButerin proposes new EIP for account abstraction targeting reduced gas costs across all L2 networks"
    assert _run_validator(headline) == 1


def test_no_input_exits_2():
    """Calling the validator with no arguments should exit 2 (usage error).

    This test invokes the script directly without passing any argument so it
    exercises the early-exit guard at the top of the script.
    """
    result = subprocess.run(
        [str(VALIDATOR)],
        capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2
