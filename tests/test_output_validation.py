"""Tests for output format validation — preamble rejection and headline validator integration."""


def test_preamble_detected(agent):
    assert agent.has_llm_preamble("Here's the headline: Ethereum hits $5000") is True
    assert agent.has_llm_preamble("Sure, here is the comment: Good article") is True
    assert agent.has_llm_preamble("Based on my analysis, the key point is...") is True
    assert agent.has_llm_preamble("I'd be happy to help. The article discusses...") is True
    assert agent.has_llm_preamble("Certainly! The main takeaway is...") is True
    assert agent.has_llm_preamble("After reading the article, here is...") is True
    assert agent.has_llm_preamble("Let me analyze this for you...") is True


def test_clean_output_no_preamble(agent):
    assert agent.has_llm_preamble("Ethereum gas fees dropped 40% after Dencun") is False
    assert agent.has_llm_preamble("$32B tokenized through ERC-3643 but...") is False
    assert agent.has_llm_preamble("Zero-fee meta-aggregation across 35 chains") is False


def test_leading_whitespace_still_detected(agent):
    """The function strips the prefix, so leading whitespace shouldn't bypass detection."""
    assert agent.has_llm_preamble("  Sure, here is the headline") is True
    assert agent.has_llm_preamble("   Here's the analysis") is True


def test_short_partial_match_not_detected(agent):
    """'Here' alone doesn't match 'here's the' — startswith requires full pattern."""
    assert agent.has_llm_preamble("Here we go again with ETH gas") is False


def test_empty_input(agent):
    assert agent.has_llm_preamble("") is False
    assert agent.has_llm_preamble(None) is False
