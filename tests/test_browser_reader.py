"""Public source rendering retains transport and process boundaries."""

import time
import subprocess
import sys

import pytest

import reply_grounding as rg
from tests.test_reply_grounding import FakePinnedConnection, FakePinnedResponse, resolver_for


STREAMED = b'<html><body><div hidden id="S:0"><article>Visible after rendering.</article></div><script>document.getElementById("S:0").hidden=false</script></body></html>'


def fetcher(body=STREAMED, *, renderer=None, content_type="text/html"):
    return rg.SafeHttpFetcher(
        resolver=resolver_for(["93.184.216.34"]),
        connection_factory=lambda *a: FakePinnedConnection(FakePinnedResponse(
            status=200, body=body, headers={"content-type": content_type})),
        renderer=renderer,
    )


def test_streamed_html_uses_renderer_without_treating_hidden_dom_as_text():
    assert rg._extract_visible_text(STREAMED, "text/html") == ""
    calls = []

    class Renderer:
        def render(self, resource, **kwargs):
            from browser_reader import RenderedPage
            calls.append((resource, kwargs))
            return RenderedPage("Visible after rendering.", len(resource.body), False)

    source = fetcher(renderer=Renderer()).fetch("https://example.com/article")
    assert source.text == "Visible after rendering."
    assert source.reader == "browser"
    assert source.truncated is False
    assert calls[0][0].body == STREAMED
    assert calls[0][1]["deadline"] > time.monotonic()


def test_static_page_does_not_spawn_renderer():
    class Renderer:
        def render(self, *a, **kw):
            pytest.fail("static article rendered unnecessarily")

    text = "An independently readable article. " * 12
    source = fetcher(f"<article>{text}</article>".encode(), renderer=Renderer()).fetch("https://example.com/article")
    assert source.reader == "http"
    assert source.text == text.strip()


def test_navigation_does_not_mask_a_dynamic_article():
    class Renderer:
        def render(self, resource, **kwargs):
            from browser_reader import RenderedPage
            return RenderedPage("The actual rendered article.", len(resource.body), False)

    body = b'<nav>' + b'Navigation links and categories. ' * 20 + b'</nav>' + STREAMED
    source = fetcher(body, renderer=Renderer()).fetch("https://example.com/article")
    assert source.reader == "browser"
    assert source.text == "The actual rendered article."


def test_navigation_does_not_mask_content_root_inserted_by_script():
    class Renderer:
        def render(self, resource, **kwargs):
            from browser_reader import RenderedPage
            return RenderedPage("The actual rendered article.", len(resource.body), False)

    body = (b'<nav>' + b'Navigation links and categories. ' * 20
            + b'</nav><div id="app"></div><script src="/app.js"></script>')
    source = fetcher(body, renderer=Renderer()).fetch("https://example.com/article")
    assert source.reader == "browser"
    assert source.text == "The actual rendered article."


def test_static_login_form_is_not_source_evidence():
    body = (b'<main><h1>Welcome back to our publication</h1><p>'
            + b'Account benefits and subscription information. ' * 8
            + b'</p><input type="password"></main>')
    with pytest.raises(rg.GroundingFailure, match="sign-in"):
        fetcher(body).fetch("https://example.com/article")


def test_final_evidence_clipping_marks_non_ascii_source_as_excerpt():
    text = "é" * 5_000
    source = rg.FetchedSource("https://example.com/long", "web:" + "a" * 20,
                              text, reader="http", truncated=False)
    evidence = rg.collect_evidence(
        {"message_id": 1, "chat": {"id": -123}, "text": source.canonical_url}, [], [],
        direct=True, mode="grounded", url_fetcher=lambda *a: source,
    )
    item = next(x for x in evidence.items if x.kind == "focal_url")
    assert item.truncated is True
    assert len(item.text.encode()) <= 8_000


def test_fetch_resource_preserves_raw_bytes_and_transport_accounting():
    calls = []
    reader = fetcher(renderer=None)
    reader.response_byte_consumer = calls.append
    resource = reader.fetch_resource("https://example.com/article")
    assert resource.body == STREAMED
    assert sum(calls) == len(STREAMED)
    assert resource.canonical_url == "https://example.com/article"


def test_reader_container_has_no_network_or_host_mounts():
    import browser_reader as br
    argv = br.container_command("benthic-reader-test")
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--user=1000:1000" in argv
    assert not any(x.startswith(("--volume", "--mount", "--env", "--privileged", "--ipc=host")) for x in argv)


def test_browser_broker_rejects_write_methods_and_unexpected_protocol_fields():
    import browser_reader as br
    for request in [
        {"type": "fetch", "url": "https://example.com", "method": "POST", "resource_type": "xhr"},
        {"type": "fetch", "url": "https://example.com", "method": "GET", "resource_type": "script", "headers": {"authorization": "secret"}},
        {"type": "fetch", "url": "file:///etc/passwd", "method": "GET", "resource_type": "script"},
    ]:
        with pytest.raises(rg.GroundingFailure):
            br.validate_resource_request(request)


@pytest.mark.parametrize("mode", ["read_timeout", "output_overflow", "write_timeout"])
def test_renderer_protocol_bounds_both_directions(mode):
    from browser_reader import _Pipe
    program = "import time; time.sleep(2)"
    if mode == "output_overflow":
        program = "import sys; sys.stdout.write('x'*70000); sys.stdout.flush()"
    process = subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    try:
        pipe = _Pipe(process, time.monotonic() + (1 if mode == "output_overflow" else 0.1))
        with pytest.raises(rg.GroundingFailure, match="too large|timed out"):
            if mode == "write_timeout":
                pipe.send({"body": "x" * 1_000_000})
            else:
                pipe.receive()
    finally:
        process.kill()
        process.wait(timeout=2)
        process.stdin.close()
        process.stdout.close()


@pytest.mark.parametrize("removed, listed, expected", [(0, b"", True), (1, b"container", False), (1, b"", True)])
def test_reaping_requires_confirmed_container_removal(monkeypatch, removed, listed, expected):
    import browser_reader as br
    class Process:
        killed = False
        def poll(self): return -9 if self.killed else None
        def wait(self, timeout):
            if timeout == 0.5: raise subprocess.TimeoutExpired("docker", timeout)
            return 0
        def kill(self): self.killed = True
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, removed if "rm" in argv else 0,
                                           stdout=listed if "ps" in argv else b"", stderr=b"")
    monkeypatch.setattr(br.subprocess, "run", run)
    assert br._reap_container(Process(), "benthic-reader-test") is expected
    assert any("rm" in call for call in calls)
