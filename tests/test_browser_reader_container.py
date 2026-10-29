"""Opt-in tests exercise the shipped reader image and real Chromium sandbox."""

import os
import time

import pytest

from browser_reader import BrowserSourceReader
from reply_grounding import GroundingFailure, HttpResource


pytestmark = pytest.mark.skipif(
    os.environ.get("BENTHIC_REPLAY_BROWSER") != "1",
    reason="requires the actual isolated browser image; set BENTHIC_REPLAY_BROWSER=1",
)
URL = "https://example.com/article"
ARTICLE = "Independent article content with enough detail to be a useful source. " * 6


def render(body, *, fetch=None, seconds=12):
    def unavailable(*args, **kwargs):
        raise GroundingFailure("fixture resource unavailable")
    return BrowserSourceReader().render(
        HttpResource(URL, body.encode(), "text/html"),
        fetch_resource=fetch or unavailable, deadline=time.monotonic() + seconds,
        max_response_bytes=2_097_152,
    )


@pytest.mark.parametrize("body", [
    f"<main hidden>{ARTICLE}</main>",
    f'<main><h1>Welcome back</h1>{ARTICLE}<input type="password"></main>',
    f"<nav>{ARTICLE}</nav><div id='app'></div>",
    "<main>Just a moment. " + ARTICLE + "</main>",
])
def test_hidden_content_and_access_shells_never_become_evidence(body):
    with pytest.raises(GroundingFailure):
        render(body)


def test_visible_article_wins_over_hidden_first_article():
    result = render(f"<article hidden>SECRET {ARTICLE}</article><article>{ARTICLE}</article>")
    assert "Independent article" in result.text
    assert "SECRET" not in result.text


def test_delayed_article_hydration_is_observed_after_intro():
    result = render(f'<main>{ARTICLE}</main><script>setTimeout(() => {{'
                    'document.querySelector("main").append(" Final hydrated conclusion.");'
                    '}, 450)</script>')
    assert "Final hydrated conclusion." in result.text


def test_unsettled_article_is_explicitly_an_excerpt():
    result = render(f'<main>{ARTICLE}</main><script>setInterval(() => {{'
                    'document.querySelector("main").append(" Updating."); }, 150)</script>')
    assert result.truncated is True


def test_utf8_capture_is_bounded_and_marked_excerpt():
    result = render("<main>" + "é" * 5_000 + "</main>")
    assert result.truncated is True
    assert len(result.text.encode()) <= 8_000
    assert "\ufffd" not in result.text


def test_redirected_script_uses_brokered_final_bytes():
    requested = []
    def fetch(url, **kwargs):
        requested.append(url)
        assert url == "https://example.com/loader.js"
        return HttpResource("https://example.com/final.js",
                            b'document.querySelector("main").hidden=false;',
                            "text/javascript")
    result = render(f'<main hidden>{ARTICLE}</main><script src="/loader.js"></script>', fetch=fetch)
    assert "Independent article" in result.text
    assert requested == ["https://example.com/loader.js"]
    assert result.truncated is True


def test_expired_render_is_reaped_and_capacity_returns():
    with pytest.raises(GroundingFailure):
        render(f"<main>{ARTICLE}</main>", seconds=.1)
    assert "Independent article" in render(f"<main>{ARTICLE}</main>").text
