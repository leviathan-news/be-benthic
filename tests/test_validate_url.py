"""Tests for validate_url() — URL validation for LLM-returned content."""


def test_valid_urls_pass(agent):
    assert agent.validate_url("https://cointelegraph.com/news/article") is not None
    assert agent.validate_url("http://example.com") is not None
    assert agent.validate_url("https://x.com/user/status/123456") is not None


def test_empty_and_none(agent):
    assert agent.validate_url("") is None
    assert agent.validate_url(None) is None


def test_non_http_schemes_rejected(agent):
    assert agent.validate_url("javascript:alert(1)") is None
    assert agent.validate_url("data:text/html,<script>alert(1)</script>") is None
    assert agent.validate_url("ftp://files.example.com/secret") is None
    assert agent.validate_url("file:///etc/passwd") is None


def test_control_characters_rejected(agent):
    assert agent.validate_url("https://evil.com/path\nnewline") is None
    assert agent.validate_url("https://evil.com/path\r\nheader: injection") is None
    assert agent.validate_url("https://evil.com/\x00null") is None
    assert agent.validate_url("https://evil.com/path\ttab") is None


def test_spaces_rejected(agent):
    assert agent.validate_url("https://evil.com/path with spaces") is None


def test_oversized_url_rejected(agent):
    long_url = "https://example.com/" + "a" * 2100
    assert agent.validate_url(long_url) is None


def test_missing_netloc_rejected(agent):
    assert agent.validate_url("https://") is None
    assert agent.validate_url("https:///path") is None


def test_quotes_stripped(agent):
    assert agent.validate_url('"https://example.com"') == "https://example.com"
    assert agent.validate_url("'https://example.com'") == "https://example.com"


def test_angle_brackets_stripped(agent):
    result = agent.validate_url("<https://example.com>")
    assert result == "https://example.com"
    assert "<" not in result
