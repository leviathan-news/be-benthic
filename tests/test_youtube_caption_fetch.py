from dataclasses import dataclass
import math

import pytest
import requests

from scripts import youtube_caption_fetch as helper


@dataclass
class Snippet:
    text: str
    start: float
    duration: float


@dataclass
class FakeTranscript:
    language: str
    language_code: str
    is_generated: bool
    snippets: tuple

    def fetch(self):
        return self.snippets


@pytest.mark.parametrize(("rows", "expected"), (
    ((FakeTranscript("Italian", "it", False, ()),
      FakeTranscript("English auto", "en", True, ()),
      FakeTranscript("English", "en-US", False, ())), "en-US"),
    ((FakeTranscript("Italian", "it", False, ()),
      FakeTranscript("English auto", "en", True, ())), "en"),
    ((FakeTranscript("Zulu", "zu", False, ()),
      FakeTranscript("Italian", "it", False, ())), "it"),
    ((FakeTranscript("Zulu auto", "zu", True, ()),
      FakeTranscript("Italian auto", "it", True, ())), "it"),
))
def test_select_transcript_uses_deterministic_preference(rows, expected):
    assert helper.select_transcript(rows).language_code == expected


def test_select_transcript_rejects_an_empty_listing():
    with pytest.raises(helper.CaptionContractError):
        helper.select_transcript(())


def test_success_payload_is_bounded_and_reports_coverage():
    selected = FakeTranscript(
        "English",
        "en",
        False,
        tuple(
            Snippet(f"line {i} " + "x" * 80, i * 5.0, 5.0)
            for i in range(20)
        ),
    )
    payload = helper.build_success_payload(
        "Noo0NWD0gHU", selected, 500, response_bytes=1_234
    )
    assert set(payload) == {
        "ok", "video_id", "language", "language_code", "is_generated",
        "truncated", "covered_seconds", "total_seconds", "caption_text",
        "response_bytes",
    }
    assert payload["truncated"] is True
    assert payload["covered_seconds"] < payload["total_seconds"]
    assert len(payload["caption_text"].encode()) <= 500


def test_success_payload_sanitizes_and_deduplicates_adjacent_lines():
    selected = FakeTranscript(
        "English",
        "en",
        False,
        (
            Snippet(" first\n line ", 0.0, 1.0),
            Snippet("first line", 1.0, 1.0),
            Snippet("second\tline", 2.0, 1.0),
        ),
    )
    payload = helper.build_success_payload(
        "Noo0NWD0gHU", selected, 1_000, response_bytes=10
    )
    assert payload["caption_text"] == (
        "[00:00:00] first line\n[00:00:02] second line"
    )
    assert payload["covered_seconds"] == 3.0
    assert payload["total_seconds"] == 3.0
    assert payload["truncated"] is False


def test_success_payload_rejects_boolean_response_byte_count():
    selected = FakeTranscript(
        "English", "en", False, (Snippet("ok", 0.0, 1.0),)
    )
    with pytest.raises(helper.CaptionContractError):
        helper.build_success_payload(
            "Noo0NWD0gHU", selected, 1_000, response_bytes=True
        )


def test_success_payload_counts_json_escape_expansion_in_caption_cap():
    selected = FakeTranscript(
        "English",
        "en",
        False,
        (
            Snippet("first", 0.0, 1.0),
            Snippet('"' * 300, 1.0, 1.0),
        ),
    )
    payload = helper.build_success_payload(
        "Noo0NWD0gHU", selected, 500, response_bytes=10
    )
    assert payload["truncated"] is True
    assert payload["caption_text"] == "[00:00:00] first"


@pytest.mark.parametrize("snippets", (
    (Snippet("bad", math.nan, 1.0),),
    (Snippet("bad", math.inf, 1.0),),
    (Snippet("bad", 1.0, -1.0),),
    (Snippet("later", 2.0, 1.0), Snippet("earlier", 1.0, 1.0)),
    (Snippet("", 0.0, 1.0),),
))
def test_success_payload_rejects_malformed_snippets(snippets):
    selected = FakeTranscript("English", "en", False, snippets)
    with pytest.raises(helper.CaptionContractError):
        helper.build_success_payload(
            "Noo0NWD0gHU", selected, 1_000, response_bytes=10
        )


@pytest.mark.parametrize(("video_id", "language", "language_code"), (
    ("short", "English", "en"),
    ("Noo0NWD0gHU", "", "en"),
    ("Noo0NWD0gHU", "English", ""),
))
def test_success_payload_rejects_invalid_identity_metadata(
        video_id, language, language_code):
    selected = FakeTranscript(
        language,
        language_code,
        False,
        (Snippet("ok", 0.0, 1.0),),
    )
    with pytest.raises(helper.CaptionContractError):
        helper.build_success_payload(
            video_id, selected, 1_000, response_bytes=10
        )


class FakeResponse:
    def __init__(self, status, chunks=(), location=None):
        self.status_code = status
        self._chunks = tuple(chunks)
        self.headers = {} if location is None else {"location": location}
        self._content = False
        self._content_consumed = False
        self.close_calls = 0

    @property
    def content(self):
        return self._content

    def iter_content(self, chunk_size):
        assert chunk_size == 64 * 1024
        yield from self._chunks

    def close(self):
        self.close_calls += 1


@pytest.mark.parametrize(("method", "url"), (
    ("DELETE", "https://www.youtube.com/watch?v=Noo0NWD0gHU"),
    ("GET", "http://www.youtube.com/watch?v=Noo0NWD0gHU"),
    ("GET", "https://youtube.com.attacker.test/watch?v=Noo0NWD0gHU"),
    ("GET", "https://user@www.youtube.com/watch?v=Noo0NWD0gHU"),
    ("GET", "https://www.youtube.com:444/watch?v=Noo0NWD0gHU"),
))
def test_guarded_session_rejects_untrusted_transport(method, url):
    session = helper.GuardedYouTubeSession(
        deadline=helper.time.monotonic() + 5,
        max_response_bytes=2_000_000,
    )
    with pytest.raises(helper.CaptionTransportError):
        session.request(method, url)


def test_guarded_session_disables_proxy_env():
    session = helper.GuardedYouTubeSession(
        deadline=helper.time.monotonic() + 5,
        max_response_bytes=2_000_000,
    )
    assert session.trust_env is False


def test_guarded_session_revalidates_redirect_host(monkeypatch):
    response = FakeResponse(
        302, location="https://attacker.test/captions"
    )
    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda *a, **k: response,
    )
    session = helper.GuardedYouTubeSession(
        helper.time.monotonic() + 5, 2_000_000
    )
    with pytest.raises(helper.CaptionTransportError):
        session.get("https://www.youtube.com/watch?v=Noo0NWD0gHU")
    assert response.close_calls == 1


def test_guarded_session_rejects_trailing_dot_authority_before_network(
        monkeypatch):
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        return FakeResponse(200)

    monkeypatch.setattr(requests.Session, "request", forbidden)
    session = helper.GuardedYouTubeSession(
        helper.time.monotonic() + 5, 2_000_000
    )
    with pytest.raises(helper.CaptionTransportError):
        session.get("https://www.youtube.com./watch?v=Noo0NWD0gHU")
    assert called is False


def test_guarded_session_counts_decoded_bytes_once(monkeypatch):
    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda *a, **k: FakeResponse(200, (b"abc", b"def")),
    )
    session = helper.GuardedYouTubeSession(
        helper.time.monotonic() + 5, 6
    )
    response = session.get(
        "https://www.youtube.com/watch?v=Noo0NWD0gHU"
    )
    assert response.content == b"abcdef"
    assert session.response_bytes == 6


def test_guarded_session_rejects_more_than_three_redirects(monkeypatch):
    calls = []

    def redirect(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse(302, location="/watch?v=Noo0NWD0gHU")

    monkeypatch.setattr(requests.Session, "request", redirect)
    session = helper.GuardedYouTubeSession(
        helper.time.monotonic() + 5, 2_000_000
    )
    with pytest.raises(helper.CaptionTransportError):
        session.get("https://www.youtube.com/watch?v=Noo0NWD0gHU")
    assert len(calls) == 4


def test_guarded_session_rejects_expired_deadline(monkeypatch):
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        return FakeResponse(200)

    monkeypatch.setattr(requests.Session, "request", forbidden)
    session = helper.GuardedYouTubeSession(
        helper.time.monotonic() - 1, 2_000_000
    )
    with pytest.raises(helper.CaptionTransportError):
        session.get("https://www.youtube.com/watch?v=Noo0NWD0gHU")
    assert called is False


def test_guarded_session_rejects_aggregate_response_overflow(monkeypatch):
    response = FakeResponse(200, (b"abc", b"defg"))
    monkeypatch.setattr(
        requests.Session, "request", lambda *a, **k: response
    )
    session = helper.GuardedYouTubeSession(
        helper.time.monotonic() + 5, 6
    )
    with pytest.raises(helper.CaptionTransportError):
        session.get("https://www.youtube.com/watch?v=Noo0NWD0gHU")
    assert response.close_calls == 1


def test_guarded_session_converts_only_303_post_redirect_to_get(monkeypatch):
    calls = []
    responses = iter((
        FakeResponse(303, location="/api/timedtext"),
        FakeResponse(200, (b"ok",)),
    ))

    def respond(_session, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)

    monkeypatch.setattr(requests.Session, "request", respond)
    session = helper.GuardedYouTubeSession(
        helper.time.monotonic() + 5, 2_000_000
    )
    response = session.post(
        "https://www.youtube.com/youtubei/v1/player", data=b"body"
    )
    assert response.content == b"ok"
    assert [call[0] for call in calls] == ["POST", "GET"]
    assert "data" not in calls[1][2]


def test_fetch_payload_passes_guarded_session_and_builds_receipt(
        monkeypatch):
    selected = FakeTranscript(
        "English", "en", False, (Snippet("bounded", 0.0, 1.0),)
    )
    observed = {}

    class FakeApi:
        def __init__(self, http_client):
            observed["session"] = http_client

        def list(self, video_id):
            observed["video_id"] = video_id
            return (selected,)

    payload = helper.fetch_payload(
        "Noo0NWD0gHU", 5, 2_000_000, 47_000, api_factory=FakeApi
    )
    assert payload["ok"] is True
    assert payload["caption_text"] == "[00:00:00] bounded"
    assert observed["video_id"] == "Noo0NWD0gHU"
    assert observed["session"].trust_env is False


def test_fetch_payload_returns_metadata_only_failure():
    class FailingApi:
        def __init__(self, http_client):
            self.http_client = http_client

        def list(self, video_id):
            raise RuntimeError("private failure details")

    assert helper.fetch_payload(
        "Noo0NWD0gHU", 5, 2_000_000, 47_000, api_factory=FailingApi
    ) == {
        "ok": False,
        "failure": "captions_unavailable",
        "response_bytes": 0,
    }
