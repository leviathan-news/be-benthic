#!/usr/bin/env python3
"""Fetch one public YouTube transcript through a bounded JSON-only helper."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from urllib.parse import urljoin, urlsplit

import requests


ALLOWED_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "consent.youtube.com",
    "youtubei.googleapis.com",
})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_MAX_REDIRECTS = 3
_READ_CHUNK_BYTES = 64 * 1024
_MAX_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 2_097_152
_MAX_CAPTION_BYTES = 47_000


class CaptionContractError(ValueError):
    """A local argument or transcript object violates the helper contract."""


class CaptionTransportError(ValueError):
    """A YouTube request violates a transport, byte, or deadline bound."""


def _bounded_text(value, *, max_bytes):
    """Return one printable, whitespace-normalized bounded metadata value."""
    if not isinstance(value, str):
        raise CaptionContractError("invalid text metadata")
    clean = " ".join(value.split())
    if (
        not clean
        or len(clean.encode("utf-8")) > max_bytes
        or any(not char.isprintable() for char in clean)
    ):
        raise CaptionContractError("invalid text metadata")
    return clean


def _finite_number(value, *, nonnegative=True):
    """Validate a real finite transcript timing value without bool coercion."""
    if type(value) not in {int, float}:
        raise CaptionContractError("invalid transcript timing")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise CaptionContractError("invalid transcript timing")
    return result


def _positive_int(value, *, maximum):
    """Validate an exact positive integer inside one configured bound."""
    if type(value) is not int or not 1 <= value <= maximum:
        raise CaptionContractError("invalid integer bound")
    return value


def _nonnegative_int(value, *, maximum):
    """Validate an exact nonnegative integer inside one configured bound."""
    if type(value) is not int or not 0 <= value <= maximum:
        raise CaptionContractError("invalid integer bound")
    return value


def select_transcript(rows):
    """Select manual English, generated English, then original-language rows."""
    try:
        available = tuple(rows)
        for generated, english_only in (
            (False, True),
            (True, True),
            (False, False),
            (True, False),
        ):
            candidates = sorted(
                (
                    row
                    for row in available
                    if bool(row.is_generated) is generated
                    and (
                        not english_only
                        or str(row.language_code).lower() == "en"
                        or str(row.language_code).lower().startswith("en-")
                    )
                ),
                key=lambda row: (
                    str(row.language_code).casefold(),
                    str(row.language).casefold(),
                ),
            )
            if candidates:
                return candidates[0]
    except Exception as exc:
        raise CaptionContractError("invalid transcript listing") from exc
    raise CaptionContractError("no public transcript")


def _timestamp(seconds):
    """Render a nonnegative caption start time as an integral clock label."""
    whole = int(seconds)
    hours, remainder = divmod(whole, 3_600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def build_success_payload(
        video_id, selected, max_caption_bytes, response_bytes):
    """Validate, sanitize, and render one transcript into the strict receipt."""
    if not isinstance(video_id, str) or _VIDEO_ID_RE.fullmatch(video_id) is None:
        raise CaptionContractError("invalid video identity")
    max_caption_bytes = _positive_int(
        max_caption_bytes, maximum=_MAX_CAPTION_BYTES
    )
    response_bytes = _nonnegative_int(
        response_bytes, maximum=_MAX_RESPONSE_BYTES
    )
    language = _bounded_text(selected.language, max_bytes=128)
    language_code = _bounded_text(selected.language_code, max_bytes=64)
    if type(selected.is_generated) is not bool:
        raise CaptionContractError("invalid generated flag")

    try:
        snippets = tuple(selected.fetch())
    except Exception as exc:
        raise CaptionContractError("transcript fetch failed") from exc
    if not snippets:
        raise CaptionContractError("empty transcript")

    validated = []
    previous_start = None
    total_seconds = 0.0
    for snippet in snippets:
        text = _bounded_text(snippet.text, max_bytes=_MAX_CAPTION_BYTES)
        start = _finite_number(snippet.start)
        duration = _finite_number(snippet.duration)
        if previous_start is not None and start < previous_start:
            raise CaptionContractError("transcript starts are not monotonic")
        previous_start = start
        end = start + duration
        if not math.isfinite(end):
            raise CaptionContractError("invalid transcript timing")
        total_seconds = max(total_seconds, end)
        validated.append((text, start, end))

    rendered = []
    rendered_bytes = 0
    escaped_bytes = 0
    covered_seconds = 0.0
    previous_text = None
    truncated = False
    for text, start, end in validated:
        # Exact adjacent duplicates do not consume caption bytes, but their
        # interval remains part of the represented transcript coverage.
        if text == previous_text:
            covered_seconds = max(covered_seconds, end)
            continue
        line = f"[{_timestamp(start)}] {text}"
        encoded = line.encode("utf-8")
        escaped = json.dumps(line, ensure_ascii=False)[1:-1].encode("utf-8")
        required = len(encoded) + (1 if rendered else 0)
        escaped_required = len(escaped) + (2 if rendered else 0)
        if (
            rendered_bytes + required > max_caption_bytes
            or escaped_bytes + escaped_required > max_caption_bytes
        ):
            truncated = True
            break
        rendered.append(line)
        rendered_bytes += required
        escaped_bytes += escaped_required
        covered_seconds = max(covered_seconds, end)
        previous_text = text
    if not rendered:
        raise CaptionContractError("caption byte bound cannot fit one line")

    return {
        "ok": True,
        "video_id": video_id,
        "language": language,
        "language_code": language_code,
        "is_generated": selected.is_generated,
        "truncated": truncated,
        "covered_seconds": covered_seconds,
        "total_seconds": total_seconds,
        "caption_text": "\n".join(rendered),
        "response_bytes": response_bytes,
    }


def _validated_transport_url(value):
    """Require exact HTTPS YouTube authorities without userinfo or ports."""
    if not isinstance(value, str) or not value or len(value) > 2_048:
        raise CaptionTransportError("invalid transport URL")
    if any(
        ord(char) < 32 or ord(char) == 127 or char.isspace()
        for char in value
    ):
        raise CaptionTransportError("invalid transport URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CaptionTransportError("invalid transport URL") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or host not in ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or "#" in value
    ):
        raise CaptionTransportError("untrusted YouTube transport")
    return value


class GuardedYouTubeSession(requests.Session):
    """Requests session enforcing exact hosts, redirects, bytes, and deadline."""

    def __init__(self, deadline, max_response_bytes):
        super().__init__()
        if type(deadline) not in {int, float} or not math.isfinite(deadline):
            raise CaptionContractError("invalid transport deadline")
        self.deadline = float(deadline)
        self.max_response_bytes = _positive_int(
            max_response_bytes, maximum=_MAX_RESPONSE_BYTES
        )
        self.response_bytes = 0
        # Environment proxy and certificate settings cannot alter this helper's
        # direct, hostname-verified requests transport.
        self.trust_env = False

    def _remaining_time(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CaptionTransportError("YouTube transport timed out")
        return remaining

    def _read_bounded(self, response):
        """Materialize decoded response bytes once under the aggregate cap."""
        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=_READ_CHUNK_BYTES):
                if not isinstance(chunk, bytes):
                    raise CaptionTransportError("invalid response chunk")
                if not chunk:
                    continue
                remaining = self.max_response_bytes - self.response_bytes
                if len(chunk) > remaining:
                    self.response_bytes = self.max_response_bytes
                    raise CaptionTransportError("YouTube response too large")
                body.extend(chunk)
                self.response_bytes += len(chunk)
        except CaptionTransportError:
            response.close()
            raise
        except Exception as exc:
            response.close()
            raise CaptionTransportError("YouTube response read failed") from exc
        response._content = bytes(body)
        response._content_consumed = True
        return response

    def request(self, method, url, **kwargs):
        """Issue one manually redirected GET or POST within the shared cap."""
        if not isinstance(method, str) or method.upper() not in {"GET", "POST"}:
            raise CaptionTransportError("unsupported YouTube method")
        current_method = method.upper()
        current_url = _validated_transport_url(url)
        current_kwargs = dict(kwargs)
        redirects = 0
        while True:
            remaining = self._remaining_time()
            request_kwargs = dict(current_kwargs)
            request_kwargs.pop("allow_redirects", None)
            request_kwargs.pop("stream", None)
            request_kwargs.pop("timeout", None)
            try:
                response = super().request(
                    current_method,
                    current_url,
                    allow_redirects=False,
                    stream=True,
                    timeout=remaining,
                    **request_kwargs,
                )
            except Exception as exc:
                raise CaptionTransportError(
                    "YouTube transport request failed"
                ) from exc
            response = self._read_bounded(response)
            location = response.headers.get("location")
            if response.status_code not in _REDIRECT_STATUSES or not location:
                return response
            if redirects >= _MAX_REDIRECTS:
                response.close()
                raise CaptionTransportError("too many YouTube redirects")
            try:
                next_url = _validated_transport_url(
                    urljoin(current_url, str(location))
                )
            except CaptionTransportError:
                response.close()
                raise
            response.close()
            redirects += 1
            if response.status_code == 303:
                current_method = "GET"
                for key in ("data", "json", "files"):
                    current_kwargs.pop(key, None)
            current_url = next_url


def _timeout_seconds(value):
    """Validate the helper's relative wall-clock timeout."""
    if type(value) not in {int, float}:
        raise CaptionContractError("invalid timeout")
    result = float(value)
    if not math.isfinite(result) or not 0 < result <= _MAX_TIMEOUT_SECONDS:
        raise CaptionContractError("invalid timeout")
    return result


def fetch_payload(
        video_id, timeout_seconds, max_response_bytes,
        max_caption_bytes, api_factory=None):
    """Fetch one original public transcript and return a strict tagged union."""
    if not isinstance(video_id, str) or _VIDEO_ID_RE.fullmatch(video_id) is None:
        raise CaptionContractError("invalid video identity")
    timeout_seconds = _timeout_seconds(timeout_seconds)
    max_response_bytes = _positive_int(
        max_response_bytes, maximum=_MAX_RESPONSE_BYTES
    )
    max_caption_bytes = _positive_int(
        max_caption_bytes, maximum=_MAX_CAPTION_BYTES
    )
    deadline = time.monotonic() + timeout_seconds
    session = GuardedYouTubeSession(deadline, max_response_bytes)
    if api_factory is None:
        from youtube_transcript_api import YouTubeTranscriptApi
        api_factory = YouTubeTranscriptApi
    try:
        selected = select_transcript(
            api_factory(http_client=session).list(video_id)
        )
        return build_success_payload(
            video_id,
            selected,
            max_caption_bytes,
            session.response_bytes,
        )
    except Exception:
        return {
            "ok": False,
            "failure": "captions_unavailable",
            "response_bytes": session.response_bytes,
        }


class _SilentArgumentParser(argparse.ArgumentParser):
    """Convert all parser exits into local exceptions without output."""

    def error(self, message):
        raise CaptionContractError("invalid command arguments")

    def exit(self, status=0, message=None):
        raise CaptionContractError("invalid command arguments")


def _parse_arguments(argv):
    """Parse the exact four-value helper command contract."""
    parser = _SilentArgumentParser(add_help=False)
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--timeout-seconds", required=True)
    parser.add_argument("--max-response-bytes", required=True)
    parser.add_argument("--max-caption-bytes", required=True)
    values = parser.parse_args(argv)
    try:
        timeout_seconds = float(values.timeout_seconds)
        max_response_bytes = int(values.max_response_bytes)
        max_caption_bytes = int(values.max_caption_bytes)
    except (TypeError, ValueError) as exc:
        raise CaptionContractError("invalid command arguments") from exc
    return (
        values.video_id,
        timeout_seconds,
        max_response_bytes,
        max_caption_bytes,
    )


def _apply_resource_limits():
    """Apply process address-space and CPU soft limits where supported."""
    try:
        import resource
    except ImportError:
        return
    for name, limit in (
        ("RLIMIT_AS", 256 * 1024 * 1024),
        ("RLIMIT_CPU", 30),
    ):
        resource_id = getattr(resource, name, None)
        if resource_id is None:
            continue
        try:
            soft, hard = resource.getrlimit(resource_id)
            finite_soft = limit if soft == resource.RLIM_INFINITY else soft
            finite_hard = limit if hard == resource.RLIM_INFINITY else hard
            resource.setrlimit(
                resource_id,
                (min(finite_soft, limit, finite_hard), hard),
            )
        except (OSError, ValueError):
            continue


def main(argv=None):
    """Run the helper without emitting exception details or non-JSON text."""
    try:
        arguments = _parse_arguments(
            sys.argv[1:] if argv is None else argv
        )
        _apply_resource_limits()
        payload = fetch_payload(*arguments)
        rendered = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )
    except Exception:
        return 2
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
