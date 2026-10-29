"""Strict, bounded contracts for evidence-grounded Benthic replies."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from typing import Callable, Literal, Mapping, Sequence
import unicodedata
from urllib.parse import (
    parse_qs,
    parse_qsl,
    quote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)
import uuid
import xml.etree.ElementTree as ElementTree

from task_context import TaskContext
from ln_archive import project_archive


_MAX_CLAIM_CHARS = 2_000
_X_ROOT_HOSTS = ("x.com", "twitter.com")
_X_STATUS_PATH = re.compile(
    r"^/([A-Za-z0-9_]{1,50})/status/([0-9]+)"
    r"(?:/(?:photo|video)/[0-9]+)?/?$"
)
_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_WATCH_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com",
})
_YOUTUBE_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
_YOUTUBE_ROOT_HOSTS = ("youtube.com", "youtu.be")
_XML_CONTENT_TYPES = {
    "application/atom+xml",
    "application/rss+xml",
    "application/xml",
    "text/xml",
}
_TEXT_CONTENT_TYPES = {
    "application/json",
    "application/xhtml+xml",
    "text/html",
    "text/markdown",  # Read as bounded source text, without rendering or expanding links.
    "text/plain",
} | _XML_CONTENT_TYPES
_XML_DECLARATION_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_MAX_INLINE_STYLE_CHARS = 4_096
_MAX_INLINE_DECLARATIONS = 64
_MAX_CSS_VAR_DEPTH = 8
# Work bounds, not trust bounds (spec 2026-08-12): CSS past these limits is
# accepted-unevaluated residual, the same class as external stylesheets.
_MAX_PAGE_STYLE_CHARS = 65_536
_MAX_PAGE_STYLE_RULES = 512
_MAX_PAGE_STYLE_AT_DEPTH = 4

# At-rules whose blocks never contain screen element style rules.
_NON_ELEMENT_AT_RULES = {
    "font-face", "keyframes", "-webkit-keyframes", "-moz-keyframes",
    "-o-keyframes", "page", "counter-style", "property",
    "font-feature-values", "font-palette-values",
}

# Curated always-hiding declarations beyond display/visibility, shared by
# inline styles and page rules. Values compare exactly after whitespace
# compaction and casefold; a var() in these properties is ignored (an
# unresolvable --text color must not hide anything), unlike
# display/visibility where unresolvable values over-hide.
_CSS_HIDING_EXTRA_VALUES = {
    "color": {"transparent"},
    "opacity": {"0", "0.0", ".0", "0%"},
    "font-size": {"0", "0px", "0em", "0rem", "0%"},
}

_CSS_CLASS_TOKEN_RE = re.compile(r"\.([-_A-Za-z0-9]+)")
_CSS_ID_TOKEN_RE = re.compile(r"#([-_A-Za-z0-9]+)")
_CSS_NOT_SEGMENT_RE = re.compile(r":not\(([^)]*)\)", re.I)
_CSS_ATTR_SEGMENT_RE = re.compile(r"\[[^\]]*\]")
_CSS_PSEUDO_SEGMENT_RE = re.compile(r"::?[-A-Za-z]+(?:\([^)]*\))?")
_CSS_TAG_TOKEN_RE = re.compile(r"(?<![\w.#-])([A-Za-z][-A-Za-z0-9]*)")
_CSS_AT_RULE_NAME_RE = re.compile(r"@([-A-Za-z]+)")
_CSS_CUSTOM_PROPERTY = re.compile(r"--[-_A-Za-z0-9]+")
_MAX_TWITTER_STDOUT_BYTES = 1_048_576
_MAX_TWITTER_STDERR_BYTES = 65_536
_TWITTER_PROCESS_SLOTS = threading.BoundedSemaphore(4)
_TELEGRAM_PROCESS_SLOTS = threading.BoundedSemaphore(1)
_YOUTUBE_PROCESS_SLOTS = threading.BoundedSemaphore(2)
_YOUTUBE_LANGUAGE_CODE_RE = re.compile(
    r"^[A-Za-z0-9]{1,16}(?:[-_][A-Za-z0-9]{1,16})*$"
)
_YOUTUBE_SUCCESS_KEYS = frozenset({
    "ok", "video_id", "language", "language_code", "is_generated",
    "truncated", "covered_seconds", "total_seconds", "caption_text",
    "response_bytes",
})
_YOUTUBE_FAILURE_KEYS = frozenset({
    "ok", "failure", "response_bytes",
})
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\[\]{}\"']+", re.I)
_BACKGROUND_LINE_RE = re.compile(
    r"^\s*background(?:\s+only)?\s*:\s*(https?://[^\s<>\[\]{}\"']+)\s*$",
    re.I,
)
_MARKET_TIMEFRAME_RE = re.compile(r"\b[1-9][0-9]{0,2}\s*[mhdw]\b", re.I)
_MARKET_TERM_RE = re.compile(
    r"\b(?:liquidity|fdv|market\s+cap|volume|ohlcv|dex\s+pool|"
    r"contract(?:\s+address)?|token|coin|ticker)\b",
    re.I,
)
_EVM_ASSET_ID_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_NETWORK_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
# "Unsupported claim" is ordinary editorial vocabulary. Its factual validity
# belongs to the independent verifier, not this internal-language filter.
_PUBLIC_GROUNDING_PROTOCOL_PATTERNS = (
    ("from this evidence", re.compile(r"\bfrom\s+this\s+evidence\b", re.I)),
    ("supplied evidence", re.compile(r"\b(?:the\s+)?supplied\s+evidence\b", re.I)),
    ("typed evidence", re.compile(r"\btyped\s+evidence\b", re.I)),
    ("evidence bundle", re.compile(r"\bevidence\s+bundle\b", re.I)),
    ("evidence id", re.compile(r"\bevidence[- ]ids?\b", re.I)),
    ("support matrix", re.compile(r"\bsupport[- ]matrix\b", re.I)),
    ("verification stage", re.compile(r"\bverification[- ]stage\b", re.I)),
    ("does not establish", re.compile(r"\bdoes\s+not\s+establish\b", re.I)),
    ("verifier", re.compile(r"\bverifier\b", re.I)),
)
_PUBLIC_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_RESERVED_RESEARCH_HOSTS = frozenset({
    "broadcasthost",
    "example.com",
    "example.net",
    "example.org",
    "ip6-localhost",
    "localhost",
    "localhost.localdomain",
})
_RESERVED_RESEARCH_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
    ".home.arpa",
    ".internal",
    ".invalid",
    ".local",
    ".localhost",
    ".onion",
    ".test",
)
log = logging.getLogger(__name__)
_DNS_RESOLVER_SLOTS = threading.BoundedSemaphore(4)


def _is_x_host(host: str) -> bool:
    """Match only X/Twitter roots and their dot-delimited subdomains."""
    return any(
        host == root or host.endswith("." + root)
        for root in _X_ROOT_HOSTS
    )


def _is_youtube_host(host: str) -> bool:
    """Match only YouTube roots and their dot-delimited subdomains."""
    return any(
        host == root or host.endswith("." + root)
        for root in _YOUTUBE_ROOT_HOSTS
    )


class GroundingFailure(ValueError):
    """A bounded grounding contract is malformed or unsupported."""


class YouTubeTranscriptUnavailable(GroundingFailure):
    """The exact video has no safely usable public caption receipt."""


class TelegramMessageUnavailable(GroundingFailure):
    """An exact private message cannot be read within the current chat scope."""


@dataclass(frozen=True)
class GroundingLimits:
    max_background_sources: int = 3
    max_focal_urls: int = 8
    max_source_requests: int = 10
    # Transport-only budgets (spec 2026-08-12): raised so large modern news
    # pages fit; extraction stays capped at 8,000 chars per source and
    # max_evidence_bytes overall.
    max_source_bytes: int = 4_194_304
    # Total budget: 900s Sol, 30s provider fallback, and 30s trusted fetch.
    source_collection_timeout: int = 960
    max_evidence_bytes: int = 65_536
    fetch_timeout: int = 15
    trace_retention_days: int = 14
    photo_reference_max_age: int = 1_800
    max_response_bytes: int = 1_572_864
    max_redirects: int = 3


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    kind: Literal[
        "current_message", "reply_message", "conversation_message",
        "focal_url", "background_url", "media", "runtime_receipt",
    ]
    text: str
    source_ref: str
    author: str | None = None
    timestamp: str | None = None
    url: str | None = None
    content_hash: str | None = None
    artifact_hash: str | None = None
    reader: str | None = None
    truncated: bool | None = None


@dataclass(frozen=True)
class SourceFailure:
    """A trusted retrieval outcome, never factual evidence from the failed page."""

    canonical_url: str
    source_ref: str
    failure_kind: str


@dataclass(frozen=True)
class EvidenceBundle:
    trace_id: str
    chat_id: int
    message_id: int
    direct: bool
    mode: Literal["conversation", "grounded"]
    focal_ids: tuple[str, ...]
    items: tuple[EvidenceItem, ...]
    background_source_urls: tuple[str, ...] = ()
    source_failures: tuple[SourceFailure, ...] = ()
    focal_source_urls: tuple[str, ...] = ()
    task_context: TaskContext | None = None

    def __post_init__(self):
        if self.mode not in {"conversation", "grounded"}:
            raise GroundingFailure("invalid evidence mode")
        ids = tuple(item.evidence_id for item in self.items)
        if len(ids) != len(set(ids)):
            raise GroundingFailure("duplicate evidence id in bundle")
        if any(value not in ids for value in self.focal_ids):
            raise GroundingFailure("unknown focal evidence id")

    def evidence_ids(self) -> frozenset[str]:
        """Return the immutable set used to validate claim references."""
        return frozenset(item.evidence_id for item in self.items)


def research_candidate_limit(
        evidence: EvidenceBundle,
        limits: GroundingLimits) -> int:
    """Bound researched candidates without changing explicit root reservations.

    Explicit background URLs continue to reserve final evidence-root slots.
    Discovery gets two candidates for each unreserved root, while the nominal
    request allowance prevents the model contract from exceeding the shared
    turn-level request ceiling. The transport ledger remains authoritative
    after focal requests, redirects, and failed responses spend real budget.
    """
    explicit_count = len(evidence.background_source_urls)
    remaining_slots = max(
        0, limits.max_background_sources - explicit_count
    )
    nominal_requests = max(
        0, limits.max_source_requests - explicit_count
    )
    return min(nominal_requests, remaining_slots * 2)


@dataclass(frozen=True)
class GroundedClaim:
    claim: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ComposedReply:
    decision: Literal["reply", "skip", "uncertain"]
    reply: str
    claims: tuple[GroundedClaim, ...]


@dataclass(frozen=True)
class VerificationVerdict:
    passed: bool
    unsupported_claims: tuple[str, ...]
    reason: str
    # Runtime policy only. parse_verification keeps its exact three-key
    # provider contract, so model output cannot select a publication policy.
    repair_policy: Literal["source_claims"] | None = None


@dataclass(frozen=True)
class MediaObservation:
    """One bounded observation row tied to a selected image index."""

    index: int
    observations: tuple[str, ...]
    visible_text: tuple[str, ...]


@dataclass(frozen=True)
class FetchedSource:
    canonical_url: str
    source_ref: str
    text: str
    author: str | None = None
    timestamp: str | None = None
    quoted: tuple["FetchedSource", ...] = field(default_factory=tuple)
    response_bytes: int = 0
    evidence_byte_limit: int = 8_000
    reader: str | None = None
    truncated: bool | None = None


ResearchRole = Literal["general", "identity", "market", "thesis"]


@dataclass(frozen=True)
class ResearchCandidate:
    """One untrusted model-proposed URL and its scheduling role."""

    url: str
    role: ResearchRole = "general"


@dataclass(frozen=True)
class ResearchPlan:
    """Strict discovery output used by the trusted collection scheduler."""

    market_intent: bool
    network: str | None
    asset_id: str | None
    candidates: tuple[ResearchCandidate, ...]

    @property
    def urls(self) -> tuple[str, ...]:
        """Return candidate URLs in the model-proposed order."""
        return tuple(candidate.url for candidate in self.candidates)


@dataclass(frozen=True)
class BackgroundCollectionResult:
    """Trusted discovered roots selected under the existing turn ledgers."""

    urls: tuple[str, ...]
    attempted_count: int
    covered_roles: frozenset[ResearchRole] = frozenset()
    market_required: bool = False

    @property
    def accepted_count(self) -> int:
        """Return how many fetched roots earned an evidence slot."""
        return len(self.urls)

    @property
    def market_complete(self) -> bool:
        """Return whether exact identity and market lanes are both covered."""
        return (
            not self.market_required
            or {"identity", "market"}.issubset(self.covered_roles)
        )


def _message_text(message):
    """Return one Telegram message's text without flattening related messages."""
    return str(message.get("text") or message.get("caption") or "")


def canonical_event_time(value):
    """Normalize supported event times to timezone-aware UTC ISO-8601.

    Naive, malformed, non-finite, and boolean values return ``None`` so they
    cannot accidentally support chronological claims.
    """
    parsed = None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        candidate = value.strip()
        try:
            parsed = datetime.fromisoformat(
                candidate[:-1] + "+00:00" if candidate.endswith(("Z", "z"))
                else candidate
            )
        except ValueError:
            try:
                parsed = parsedate_to_datetime(candidate)
            except (TypeError, ValueError, OverflowError):
                return None
    if parsed is None or parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(timezone.utc).isoformat()
    except (OverflowError, ValueError):
        return None


def _message_event_time(message):
    """Prefer an ingress-normalized event field when one is explicitly present."""
    value = message.get("event_time") if "event_time" in message else message.get("date")
    return canonical_event_time(value)


def conversation_topic_id(message):
    """Ordinary reply threads are not forum isolation boundaries.

    Trusted Telegram ingress supplies is_forum=False when the optional flag is
    absent. Unknown callers keep the narrower legacy scope until normalized.
    """
    if message.get("chat", {}).get("is_forum") is False:
        return 0
    return int(message.get("message_thread_id") or 0)


def conversation_link_candidates(message, recent_messages, persisted_context):
    """Return at most ten witnessed human link messages from the last 30 minutes."""
    chat_id = int(message.get("chat", {}).get("id", 0))
    current_id = int(message.get("message_id") or 0)
    timestamp = _message_event_time(message)
    if not timestamp:
        return ()
    current_time = datetime.fromisoformat(timestamp)
    nonforum = message.get("chat", {}).get("is_forum") is False
    rows = list(recent_messages)[-50:]
    for row in persisted_context[-100:]:
        if row.get("role", "incoming") != "incoming":
            continue
        rows.append({
            "message_id": row.get("message_id"),
            "chat": {"id": row.get("chat_id")},
            "message_thread_id": row.get("topic_id"),
            "from": {"id": row.get("sender_id"), "username": row.get("sender"), "is_bot": row.get("sender_is_bot")},
            "event_time": row.get("timestamp"),
            "text": row.get("text"),
            "reply_to_message": {"message_id": row.get("reply_to_msg_id")},
        })
    candidates = {}
    for row in rows:
        row_id = row.get("message_id")
        if (type(row_id) is not int or not 0 < row_id < current_id
                or row.get("chat", {}).get("id") != chat_id
                or row.get("from", {}).get("is_bot")
                or (not nonforum and conversation_topic_id(row) != conversation_topic_id(message))):
            continue
        event_time = _message_event_time(row)
        if not event_time:
            continue
        age = (current_time - datetime.fromisoformat(event_time)).total_seconds()
        if not 0 <= age <= 1800:
            continue
        text = _clean_evidence_text(_message_text(row), 2000)
        try:
            focal, _ = _classified_message_urls(text)
        except GroundingFailure:
            # An invalid older label is ineligible; it must not abort the
            # current request or make an unrelated candidate authoritative.
            continue
        if not focal:
            continue
        # Keep the freshest witnessed representation ahead of persisted text.
        candidates.setdefault(row_id, {
            "message_id": row_id,
            "chat": dict(message.get("chat", {})),
            "message_thread_id": conversation_topic_id(message) or None,
            "from": dict(row.get("from", {})),
            "event_time": event_time,
            "text": text,
            "reply_to_message_id": (row.get("reply_to_message") or {}).get("message_id"),
        })
    return tuple(candidates[key] for key in sorted(candidates)[-10:])


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Bound normalized text by UTF-8 bytes without splitting a code point."""
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    # ``ignore`` removes only an incomplete code point at the final byte
    # boundary; every complete character before that boundary is preserved.
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _clean_evidence_text(value, byte_limit):
    """Normalize and UTF-8-byte-bound one evidence item's text."""
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"\[photo#[0-9]+\]", "[photo omitted]", value)
    value = value.replace("<", "＜").replace(">", "＞")
    value = re.sub(r"[-=]{4,}", "---", value)
    value = "".join(
        char for char in value
        if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127)
    )
    return truncate_utf8(value.strip(), byte_limit)


def _content_hash(value):
    """Return a stable digest for normalized evidence text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _urls_in(value):
    """Extract unique HTTP(S) URLs in first-seen order from one focal item."""
    found = []
    for match in _HTTP_URL_RE.findall(value or ""):
        candidate = match.rstrip(".,!?;:)")
        if candidate and candidate not in found:
            found.append(candidate)
    return tuple(found)


def telegram_linked_urls(text):
    """Canonical focal links; the caller bounds and scopes message chaining."""
    focal, _ = _classified_message_urls(text)
    return tuple(dict.fromkeys(canonical_source_url(url) for url in focal))


def _classified_message_urls(value):
    """Split focal and explicitly labeled background URLs line by line.

    Only ``Background: URL`` and ``Background only: URL`` are accepted labels.
    Any other line that combines the word background with a URL is ambiguous
    and fails closed before source dispatch.
    """
    focal = []
    background = []
    for line in str(value or "").splitlines() or ("",):
        urls = _urls_in(line)
        label = _BACKGROUND_LINE_RE.fullmatch(line)
        if label is not None:
            if len(urls) != 1 or label.group(1) != urls[0]:
                raise GroundingFailure("ambiguous background URL syntax")
            background.append(urls[0])
            continue
        if urls and re.search(r"\bbackground\b", line, flags=re.I):
            raise GroundingFailure("ambiguous background URL syntax")
        focal.extend(urls)
    return tuple(focal), tuple(background)


def _telegram_ref(message, fallback_chat_id=0, suffix=""):
    """Build a stable Telegram source reference from typed message fields."""
    chat_id = int(message.get("chat", {}).get("id", fallback_chat_id))
    message_id = int(message.get("message_id", 0))
    return f"telegram:{chat_id}:{message_id}{suffix}"


def _validated_source_ref(value):
    """Require a bounded, printable, whitespace-free evidence reference."""
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 512
        or any(char.isspace() or not char.isprintable() for char in value)
    ):
        raise GroundingFailure("invalid evidence source reference")
    return value


def _evidence_item(
        evidence_id, kind, text, source_ref, *, byte_limit=None, **kwargs):
    """Construct one normalized evidence item with a content digest."""
    # Text-document media and exact YouTube caption receipts have separate
    # bounded allowances. Every other evidence type retains the 8 KB limit.
    if byte_limit is None:
        byte_limit = 48_512 if kind == "media" else 8_000
    if type(byte_limit) is not int or byte_limit not in {
        8_000, 48_000, 48_512,
    }:
        raise GroundingFailure("invalid evidence byte limit")
    if byte_limit == 48_000 and re.fullmatch(
            r"youtube:[A-Za-z0-9_-]{11}", str(source_ref or "")) is None:
        raise GroundingFailure(
            "elevated evidence byte limit is not YouTube"
        )
    if byte_limit == 48_512 and kind != "media":
        raise GroundingFailure(
            "media evidence byte limit used by non-media"
        )
    clean = _clean_evidence_text(text, byte_limit)
    if kwargs.get("truncated") is False and len(str(text).encode("utf-8")) > byte_limit:
        kwargs["truncated"] = True
    return EvidenceItem(
        evidence_id=evidence_id,
        kind=kind,
        text=clean,
        source_ref=_validated_source_ref(source_ref),
        content_hash=_content_hash(clean),
        **kwargs,
    )


def _fit_evidence(items, byte_limit):
    """Drop whole low-priority items and reject essential-item overflow."""
    if type(byte_limit) is not int or byte_limit < 0:
        raise GroundingFailure("invalid evidence byte limit")
    values = list(items)

    def size():
        return sum(len(item.text.encode("utf-8")) for item in values)

    # Runtime snapshots are useful claim support but cannot displace the
    # selected document or current task that triggered this turn.
    for kind in (
        "background_url", "conversation_message", "runtime_receipt"
    ):
        index = len(values) - 1
        while size() > byte_limit and index >= 0:
            if (values[index].kind == kind
                    and not values[index].source_ref.startswith((
                        "runtime:source_failure:", "runtime:task_context:", "runtime:onchain:"))):
                values.pop(index)
            index -= 1
    if size() > byte_limit:
        raise GroundingFailure("essential evidence exceeds byte limit")
    for item in values:
        if item.content_hash != _content_hash(item.text):
            raise GroundingFailure("evidence content hash mismatch")
    return tuple(values)


def _background_source_type(url):
    """Classify a failed source without logging its authority or body."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except (TypeError, ValueError):
        return "http"
    if _is_x_host(host):
        return "twitter"
    return "youtube" if _is_youtube_host(host) else "http"


def collect_evidence(
    message: dict,
    recent_messages: Sequence[dict],
    persisted_context: Sequence[Mapping[str, object]],
    *,
    direct: bool,
    mode: str,
    url_fetcher: Callable[[str, bool], FetchedSource],
    background_urls: Sequence[str] = (),
    media_items: Sequence[EvidenceItem] = (),
    runtime_receipts: Sequence[EvidenceItem] = (),
    referenced_message: Mapping[str, object] | None = None,
    task_context: TaskContext | None = None,
    allow_cross_chat_context: bool = False,
    limits: GroundingLimits | None = None,
    trace_id: str | None = None,
) -> EvidenceBundle:
    """Assemble bounded, typed, turn-local evidence for one reply."""
    limits = limits or GroundingLimits()
    chat_id = int(message.get("chat", {}).get("id", 0))
    message_id = int(message.get("message_id", 0))
    current_text = _message_text(message) or "[media attached]"
    items = [_evidence_item(
        "M0",
        "current_message",
        current_text,
        _telegram_ref(message),
        author=str(message.get("from", {}).get("username") or "")[:64] or None,
        timestamp=_message_event_time(message),
    )]
    reply = message.get("reply_to_message")
    if isinstance(reply, dict) and reply.get("message_id"):
        items.append(_evidence_item(
            "R1",
            "reply_message",
            _message_text(reply),
            _telegram_ref(reply, chat_id),
            author=str(reply.get("from", {}).get("username") or "")[:64] or None,
            timestamp=_message_event_time(reply),
        ))

    reference = None
    if task_context is not None:
        if not isinstance(task_context, TaskContext) or not task_context.valid_for(message):
            raise GroundingFailure("task reference is unavailable")
        # Receipt restoration reread each original row and verified its sender,
        # scope, age and hash. Repeat immutable bounds at collection. M0 stays
        # authoritative; referenced requests only explain this continuation.
        task_item_ids = {message_id: "M0"}
        reference_index = 2
        for witnessed in (*task_context.sources, *task_context.requests):
            if witnessed.message_id in task_item_ids:
                continue
            evidence_id = f"R{reference_index}"
            reference_index += 1
            task_item_ids[witnessed.message_id] = evidence_id
            items.append(_evidence_item(
                evidence_id, "reply_message", witnessed.text,
                f"telegram:{chat_id}:{witnessed.message_id}",
                author=str(witnessed.sender_id), timestamp=witnessed.event_time,
            ))
        context_text = json.dumps({
            "current_request": "M0",
            "original_requested_action": task_item_ids[task_context.root_message_id],
            "request_history": [task_item_ids[item.message_id] for item in task_context.requests],
            "source_messages": [task_item_ids[item.message_id] for item in task_context.sources],
            "continued_via_sent_reply": task_context.reply_message_id is not None,
            "scope": "Original action, source identity and version constraints interpret this continuation only. M0 supersedes changed requirements. Source bodies must support the answer.",
        }, ensure_ascii=False)
        runtime_receipts = (*runtime_receipts, EvidenceItem(
            "", "runtime_receipt", context_text,
            "runtime:task_context:" + _content_hash(context_text)[:20],
        ))
    elif referenced_message is not None:
        # Repeat eligibility at collection; a model-selected ID cannot supply
        # its own text, chat, timestamp, or URL to the authenticated lookup.
        reference = next((candidate for candidate in conversation_link_candidates(
            message, recent_messages, persisted_context
        ) if candidate["message_id"] == referenced_message.get("message_id")), None)
        if reference is None:
            raise GroundingFailure("referenced message is unavailable")
        items.append(_evidence_item(
            "R2", "reply_message", reference["text"], _telegram_ref(reference),
            author=str(reference.get("from", {}).get("username") or "")[:64] or None,
            timestamp=_message_event_time(reference),
        ))

    current_topic = conversation_topic_id(message)
    seen_context = {(chat_id, message_id, "incoming")}
    if isinstance(reply, dict):
        seen_context.add((chat_id, int(reply.get("message_id") or 0), "incoming"))
    if reference is not None:
        seen_context.add((chat_id, reference["message_id"], "incoming"))
    if task_context is not None:
        seen_context.update((chat_id, item.message_id, "incoming")
                            for item in (*task_context.sources, *task_context.requests))
    context_rows = []
    for row in list(recent_messages)[-20:]:
        context_rows.append({
            "role": "incoming",
            "message_id": row.get("message_id"),
            "chat_id": row.get("chat", {}).get("id", chat_id),
            "sender": row.get("from", {}).get("username") or "?",
            "sender_is_bot": bool(row.get("from", {}).get("is_bot")),
            "text": _message_text(row),
            "timestamp": _message_event_time(row),
            "topic_id": row.get("message_thread_id"),
        })
    context_rows.extend(dict(row) for row in persisted_context[-50:])
    context_index = 0
    for row in context_rows:
        row_chat = int(row.get("chat_id") or chat_id)
        row_message = int(row.get("message_id") or 0)
        row_topic = (
            0 if message.get("chat", {}).get("is_forum") is False and row_chat == chat_id
            else int(row.get("topic_id") or 0)
        )
        role = str(row.get("role") or "incoming")
        key = (row_chat, row_message, role)
        wrong_scope = (
            row_chat != chat_id or row_topic != current_topic
        ) and not allow_cross_chat_context
        if wrong_scope or key in seen_context or not row.get("text"):
            continue
        seen_context.add(key)
        context_index += 1
        suffix = ":bot_reply" if role == "our_reply" else ""
        items.append(_evidence_item(
            f"C{context_index}",
            "conversation_message",
            row.get("text"),
            f"telegram:{row_chat}:{row_message}{suffix}",
            author=str(row.get("sender") or "?")[:64],
            timestamp=canonical_event_time(row.get("timestamp")),
        ))

    focal_urls, explicit_background_urls = _classified_message_urls(current_text)
    focal_urls = list(focal_urls)
    explicit_background_urls = list(explicit_background_urls)
    if isinstance(reply, dict) and not reply.get("from", {}).get("is_bot"):
        reply_focal, reply_background = _classified_message_urls(
            _message_text(reply)
        )
        focal_urls.extend(reply_focal)
        explicit_background_urls.extend(reply_background)
    if reference is not None:
        reference_focal, _ = _classified_message_urls(reference["text"])
        focal_urls.extend(reference_focal)
    if task_context is not None:
        for source_message in task_context.sources:
            source_focal, source_background = _classified_message_urls(source_message.text)
            focal_urls.extend(source_focal)
            explicit_background_urls.extend(source_background)

    def canonical_unique(values):
        canonical = []
        for value in values:
            normalized = _canonical_source_url(value)
            if normalized not in canonical:
                canonical.append(normalized)
        return canonical

    focal_urls = canonical_unique(focal_urls)
    explicit_background_urls = canonical_unique(explicit_background_urls)
    explicit_background_set = set(explicit_background_urls)
    focal_urls = [
        value for value in focal_urls if value not in explicit_background_set
    ]
    if len(focal_urls) > limits.max_focal_urls:
        raise GroundingFailure("too many focal URLs")
    discovered_background_urls = canonical_unique(background_urls)
    all_background_urls = list(explicit_background_urls)
    for value in discovered_background_urls:
        if value not in all_background_urls and value not in focal_urls:
            all_background_urls.append(value)
    if len(all_background_urls) > limits.max_background_sources:
        raise GroundingFailure("too many background URLs")

    focal_ids = []
    source_refs = set()
    focal_index = 0
    source_failures = []
    first_failure = None
    for url in focal_urls:
        try:
            source = url_fetcher(url, True)
        except GroundingFailure as exc:
            # A link is a dependency of the claims about it, not of every
            # independent part of the user's request. Keep successful sources.
            if first_failure is None:
                first_failure = exc
            failure_kind = (
                "telegram_message_unavailable" if isinstance(exc, TelegramMessageUnavailable)
                else "youtube_captions_unavailable" if isinstance(exc, YouTubeTranscriptUnavailable)
                else "source_unavailable"
            )
            source_failures.append(SourceFailure(
                url, "web:" + hashlib.sha256(url.encode()).hexdigest()[:20], failure_kind,
            ))
            continue
        for candidate in (source, *source.quoted):
            candidate_ref = _validated_source_ref(candidate.source_ref)
            if candidate_ref in source_refs:
                continue
            source_refs.add(candidate_ref)
            focal_index += 1
            evidence_id = f"F{focal_index}"
            focal_ids.append(evidence_id)
            items.append(_evidence_item(
                evidence_id,
                "focal_url",
                candidate.text,
                candidate_ref,
                byte_limit=candidate.evidence_byte_limit,
                author=candidate.author,
                timestamp=canonical_event_time(candidate.timestamp),
                url=candidate.canonical_url,
                reader=candidate.reader,
                truncated=candidate.truncated,
            ))

    from onchain_evidence import usable_receipt as usable_onchain_receipt
    onchain_source_available = any(
        usable_onchain_receipt(item, url=url)
        for item in runtime_receipts for url in focal_urls
    )
    if focal_urls and not focal_ids and not onchain_source_available:
        # Unrelated discovery must never stand in for an entirely unreadable
        # requested source. Preserve the adapter-specific direct failure.
        raise first_failure or GroundingFailure("focal source contains no evidence")

    background_index = 0
    for url in all_background_urls:
        try:
            source = url_fetcher(url, False)
        except GroundingFailure:
            log.warning(
                "Grounding source omitted: role=background "
                "source_type=%s failure_code=source_unavailable",
                _background_source_type(url),
            )
            continue
        for candidate in (source, *source.quoted):
            candidate_ref = _validated_source_ref(candidate.source_ref)
            if candidate_ref in source_refs:
                continue
            source_refs.add(candidate_ref)
            background_index += 1
            items.append(_evidence_item(
                f"B{background_index}",
                "background_url",
                candidate.text,
                candidate_ref,
                byte_limit=candidate.evidence_byte_limit,
                author=candidate.author,
                timestamp=canonical_event_time(candidate.timestamp),
                url=candidate.canonical_url,
                reader=candidate.reader,
                truncated=candidate.truncated,
            ))

    for index, item in enumerate(media_items, 1):
        if not isinstance(item, EvidenceItem):
            raise GroundingFailure("invalid media evidence item")
        items.append(_evidence_item(
            f"P{index}",
            "media",
            item.text,
            item.source_ref,
            author=item.author,
            timestamp=canonical_event_time(item.timestamp),
            url=item.url,
            artifact_hash=item.artifact_hash,
        ))
    failure_receipts = []
    for failure in source_failures:
        receipt_text = json.dumps({
            "requested_source": failure.canonical_url,
            "retrieval_outcome": failure.failure_kind,
            "scope": "This turn only. No content or facts were obtained from this source.",
        }, ensure_ascii=False)
        failure_receipts.append(EvidenceItem(
            "", "runtime_receipt", receipt_text,
            "runtime:source_failure:" + _content_hash(receipt_text)[:20],
        ))
    for index, item in enumerate((*runtime_receipts, *failure_receipts), 1):
        if not isinstance(item, EvidenceItem):
            raise GroundingFailure("invalid runtime receipt")
        items.append(_evidence_item(
            f"T{index}",
            "runtime_receipt",
            item.text,
            item.source_ref,
            author=item.author,
            timestamp=canonical_event_time(item.timestamp),
            url=item.url,
        ))
    fitted = _fit_evidence(items, limits.max_evidence_bytes)
    return EvidenceBundle(
        trace_id=trace_id or uuid.uuid4().hex,
        chat_id=chat_id,
        message_id=message_id,
        direct=direct,
        mode=mode,
        focal_ids=tuple(focal_ids),
        items=fitted,
        source_failures=tuple(source_failures),
        focal_source_urls=tuple(focal_urls),
        background_source_urls=tuple(all_background_urls),
        task_context=task_context,
    )


def _embedded_ipv4_addresses(value):
    """Return IPv4 addresses carried by supported IPv6 transition formats."""
    if not isinstance(value, ipaddress.IPv6Address):
        return ()
    embedded = []
    if value.ipv4_mapped is not None:
        embedded.append(value.ipv4_mapped)
    if value.sixtofour is not None:
        embedded.append(value.sixtofour)
    if value.teredo is not None:
        embedded.extend(value.teredo)
    if value in _NAT64_WELL_KNOWN_PREFIX:
        embedded.append(ipaddress.IPv4Address(int(value) & 0xFFFFFFFF))
    return tuple(dict.fromkeys(embedded))


def _is_public_address(value):
    """Require both an address and every embedded transition address to be public."""
    if not value.is_global or value.is_multicast:
        return False
    return all(
        embedded.is_global and not embedded.is_multicast
        for embedded in _embedded_ipv4_addresses(value)
    )


def resolve_public_addresses(host, port, *, resolver=socket.getaddrinfo):
    """Resolve every address and reject the complete answer if any is unsafe."""
    try:
        rows = resolver(host, port, type=socket.SOCK_STREAM)
        addresses = tuple(dict.fromkeys(str(row[4][0]) for row in rows))
    except (OSError, TypeError, ValueError, IndexError) as exc:
        raise GroundingFailure("hostname resolution failed") from exc
    if not addresses:
        raise GroundingFailure("hostname did not resolve")
    try:
        parsed = tuple(ipaddress.ip_address(value) for value in addresses)
    except ValueError as exc:
        raise GroundingFailure("hostname returned an invalid address") from exc
    if any(not _is_public_address(value) for value in parsed):
        raise GroundingFailure("hostname resolved to non-public address")
    return addresses


def _resolve_before_deadline(host, port, resolver, timeout):
    """Bound resolver work by both a deadline and a process-wide worker cap."""
    wait = max(0.0, float(timeout))
    deadline = time.monotonic() + wait
    acquired = _DNS_RESOLVER_SLOTS.acquire(timeout=wait)
    if not acquired:
        raise GroundingFailure("source DNS resolver saturated")
    values = []
    errors = []

    def run():
        try:
            values.append(resolve_public_addresses(
                host, port, resolver=resolver
            ))
        except Exception as exc:
            errors.append(exc)
        finally:
            _DNS_RESOLVER_SLOTS.release()

    worker = threading.Thread(target=run, daemon=True)
    try:
        worker.start()
    except Exception:
        _DNS_RESOLVER_SLOTS.release()
        raise
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        raise GroundingFailure("source DNS lookup timed out")
    if errors:
        error = errors[0]
        if isinstance(error, GroundingFailure):
            raise error
        raise GroundingFailure("hostname resolution failed") from error
    return values[0]


def _connect_pinned_socket(ip, port, timeout):
    """Connect a numeric validated address without hostname resolution."""
    parsed = ipaddress.ip_address(ip)
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    sockaddr = (
        (str(parsed), port, 0, 0)
        if family == socket.AF_INET6
        else (str(parsed), port)
    )
    connected = socket.socket(family, socket.SOCK_STREAM)
    try:
        connected.settimeout(timeout)
        connected.connect(sockaddr)
        return connected
    except Exception:
        connected.close()
        raise


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connect plain HTTP directly to the address validated by DNS policy."""

    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self._validated_ip = ip

    def connect(self):
        self.sock = _connect_pinned_socket(
            self._validated_ip, self.port, self.timeout
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a pinned address while verifying TLS against the hostname."""

    def __init__(self, host, ip, port, timeout):
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._validated_ip = ip

    def connect(self):
        raw = _connect_pinned_socket(
            self._validated_ip, self.port, self.timeout
        )
        try:
            self.sock = self._context.wrap_socket(
                raw, server_hostname=self.host
            )
        except Exception:
            raw.close()
            raise


def _default_connection_factory(scheme, host, ip, port, timeout):
    """Construct a direct transport without consulting proxy environment state."""
    cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    return cls(host, ip, port, timeout)


def _strip_css_comments(value, maximum=_MAX_INLINE_STYLE_CHARS):
    """Remove bounded CSS comments and reject an unterminated comment."""
    if len(value) > maximum:
        return None
    pieces = []
    cursor = 0
    while cursor < len(value):
        start = value.find("/*", cursor)
        if start < 0:
            pieces.append(value[cursor:])
            break
        pieces.append(value[cursor:start])
        end = value.find("*/", start + 2)
        if end < 0:
            return None
        cursor = end + 2
    return "".join(pieces)


def _decode_css_escapes(value):
    """Decode bounded CSS hexadecimal and single-character escapes."""
    if len(value) > _MAX_INLINE_STYLE_CHARS:
        return None
    decoded = []
    cursor = 0
    hex_digits = "0123456789abcdefABCDEF"
    whitespace = " \t\r\n\f"
    while cursor < len(value):
        char = value[cursor]
        if char != "\\":
            decoded.append(char)
            cursor += 1
            continue
        cursor += 1
        if cursor >= len(value):
            return None
        char = value[cursor]
        if char in "\r\n\f":
            if (
                char == "\r"
                and cursor + 1 < len(value)
                and value[cursor + 1] == "\n"
            ):
                cursor += 2
            else:
                cursor += 1
            continue
        if char in hex_digits:
            start = cursor
            while (
                cursor < len(value)
                and cursor - start < 6
                and value[cursor] in hex_digits
            ):
                cursor += 1
            codepoint = int(value[start:cursor], 16)
            if cursor < len(value) and value[cursor] in whitespace:
                if (
                    value[cursor] == "\r"
                    and cursor + 1 < len(value)
                    and value[cursor + 1] == "\n"
                ):
                    cursor += 2
                else:
                    cursor += 1
            if (
                codepoint == 0
                or codepoint > 0x10FFFF
                or 0xD800 <= codepoint <= 0xDFFF
            ):
                decoded.append("\uFFFD")
            else:
                decoded.append(chr(codepoint))
            continue
        decoded.append(char)
        cursor += 1
    return "".join(decoded)


def _css_value_importance(value):
    """Separate a decoded trailing important marker from a declaration value."""
    match = re.search(r"!\s*important\s*$", value, flags=re.I)
    if match is None:
        return value, False
    return value[:match.start()], True


def _store_css_declaration(target, name, value, important):
    """Apply inline declaration order while preserving important precedence."""
    current = target.get(name)
    if current is None or important or not current[1]:
        target[name] = (value, important)


def _parse_inline_style(value):
    """Parse only custom, display, and visibility inline declarations."""
    without_comments = _strip_css_comments(value)
    if without_comments is None:
        return None
    declarations = without_comments.split(";")
    if len(declarations) > _MAX_INLINE_DECLARATIONS:
        return None
    standard = {}
    custom = {}
    for declaration in declarations:
        if not declaration.strip() or ":" not in declaration:
            continue
        raw_name, raw_value = declaration.split(":", 1)
        name = _decode_css_escapes(raw_name)
        decoded_value = _decode_css_escapes(raw_value)
        if name is None or decoded_value is None:
            return None
        name = name.strip()
        decoded_value, important = _css_value_importance(decoded_value)
        if name.startswith("--"):
            if _CSS_CUSTOM_PROPERTY.fullmatch(name):
                _store_css_declaration(
                    custom, name, decoded_value, important
                )
            continue
        normalized_name = name.casefold()
        if (
            normalized_name in {"display", "visibility"}
            or normalized_name in _CSS_HIDING_EXTRA_VALUES
        ):
            _store_css_declaration(
                standard, normalized_name, decoded_value, important
            )
        # Other properties cannot hide via the modeled channels and are
        # ignored (spec 2026-08-12): exotic hiding (offscreen text-indent,
        # clip paths) is accepted residual, matching page-rule and
        # external-CSS semantics. Undecodable declarations still return None
        # above, which every caller treats as hiding.
    return standard, custom


def _resolve_inline_css_value(value, custom, seen=(), depth=0, external_custom=()):
    """Resolve local properties and bounded fallbacks without guessing inheritance."""
    compact = "".join(value.split())
    match = re.fullmatch(
        r"(?i:var)\((--[-_A-Za-z0-9]+)(?:,(.+))?\)", compact
    )
    if match is None:
        return None if "var(" in compact.casefold() else compact
    name = match.group(1)
    if depth >= _MAX_CSS_VAR_DEPTH or name in seen:
        return None
    if name in custom:
        candidate = custom[name][0]
        if candidate.strip().casefold() in {"initial", "inherit", "unset", "revert", "revert-layer"}:
            # CSS-wide custom-property values can invalidate the variable or
            # inherit hiding from outside this declaration. Do not resolve
            # them as literal visible display values.
            return None
    else:
        # A declaration elsewhere may be inherited. Its absence from this
        # single rule does not justify using a visible fallback instead.
        if name in external_custom or match.group(2) is None:
            return None
        candidate = match.group(2)
    return _resolve_inline_css_value(
        candidate, custom, seen + (name,), depth + 1, external_custom
    )


def _declarations_hide(standard, custom, external_custom=()):
    """Evaluate watched declarations; True when any evaluably hides."""
    for property_name, declaration in standard.items():
        resolved = _resolve_inline_css_value(
            declaration[0], custom, external_custom=external_custom
        )
        if property_name in ("display", "visibility"):
            if resolved is None:
                return True
            normalized = resolved.casefold()
            if property_name == "display" and normalized == "none":
                return True
            if property_name == "visibility" and normalized in {
                "hidden", "collapse",
            }:
                return True
            continue
        if resolved is None:
            continue
        if resolved.casefold() in _CSS_HIDING_EXTRA_VALUES[property_name]:
            return True
    return False


def _inline_style_is_hidden(value, external_custom=()):
    """Evaluate bounded inline declarations with skip-unknown semantics."""
    if not value:
        return False
    parsed = _parse_inline_style(value)
    if parsed is None:
        return True
    standard, custom = parsed
    return _declarations_hide(standard, custom, external_custom)


def _screen_media_applies(value):
    """Return whether a bounded stylesheet media attribute can affect screen text."""
    if not value:
        return True
    media = {part.strip().casefold() for part in value.split(",") if part.strip()}
    return not media or media != {"print"}


def _html_attribute_map(attrs):
    """Build a case-insensitive map where the first occurrence wins.

    Browsers keep the first duplicate attribute; hide-relevant attributes are
    additionally checked across EVERY occurrence by _VisibleTextParser so a
    hiding duplicate can never be shadowed (spec 2026-08-12).
    """
    values = {}
    for key, value in attrs:
        name = str(key).casefold()
        if name not in values:
            values[name] = str(value or "")
    return values


def _attr_occurrences(attrs, name):
    """Yield every raw occurrence of one case-insensitive HTML attribute."""
    for key, value in attrs:
        if str(key).casefold() == name:
            yield str(value or "")


class _PageStyleScanner(HTMLParser):
    """Collect bounded inline screen CSS and detect external screen stylesheets."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._capture_style = False
        self._style_depth = 0
        self.parts = []
        self.external_screen_stylesheet = False
        self.custom_properties = set()

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        values = _html_attribute_map(attrs)
        for value in _attr_occurrences(attrs, "style"):
            parsed = _parse_inline_style(value)
            if parsed is not None:
                self.custom_properties.update(parsed[1])
        if tag == "link":
            rel = {part.casefold() for part in values.get("rel", "").split()}
            if "stylesheet" in rel and _screen_media_applies(values.get("media", "")):
                self.external_screen_stylesheet = True
        if tag == "style":
            self._style_depth += 1
            style_type = values.get("type", "text/css").strip().casefold()
            self._capture_style = (
                self._style_depth == 1
                and style_type in {"", "text/css"}
                and _screen_media_applies(values.get("media", ""))
            )

    def handle_endtag(self, tag):
        if tag.casefold() == "style" and self._style_depth:
            self._style_depth -= 1
            if self._style_depth == 0:
                self._capture_style = False

    def handle_data(self, data):
        if self._capture_style:
            self.parts.append(data)


def _strip_page_css_comments(value):
    """Best-effort comment strip: an unterminated comment drops only its tail."""
    value = value[:_MAX_PAGE_STYLE_CHARS]
    pieces = []
    cursor = 0
    while cursor < len(value):
        start = value.find("/*", cursor)
        if start < 0:
            pieces.append(value[cursor:])
            break
        pieces.append(value[cursor:start])
        end = value.find("*/", start + 2)
        if end < 0:
            break
        cursor = end + 2
    return "".join(pieces)


def _css_scan(css):
    """Split one CSS level into (prelude, block|None) segments, quote-aware.

    A stray close brace or an unterminated block/string abandons the rest of
    the sheet: everything already split still applies, the tail is residual.
    """
    segments = []
    prelude_start = 0
    block_start = -1
    depth = 0
    quote = ""
    index = 0
    length = len(css)
    while index < length:
        char = css[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if char == "{":
            if depth == 0:
                block_start = index
            depth += 1
            index += 1
            continue
        if char == "}":
            if depth == 0:
                return segments
            depth -= 1
            if depth == 0:
                segments.append((
                    css[prelude_start:block_start],
                    css[block_start + 1:index],
                ))
                prelude_start = index + 1
            index += 1
            continue
        if char == ";" and depth == 0:
            segments.append((css[prelude_start:index], None))
            prelude_start = index + 1
            index += 1
            continue
        index += 1
    return segments


def _media_never_canonical(prelude):
    """True when no comma alternative can affect the canonical viewer.

    The gate models what a light-scheme screen reader sees: print-only and
    dark-scheme-only alternatives cannot affect that rendering, so their
    blocks are skipped. Anything with a "not" (or otherwise ambiguous)
    over-applies. Skipping dark branches is what keeps theme-duplicated DOM
    readable — over-applying both scheme branches hides both page copies.
    """
    conditions = prelude.split("@media", 1)[-1]
    for part in conditions.split(","):
        tokens = set(re.findall(r"[a-z-]+", part.casefold()))
        if "not" in tokens:
            return False
        print_only = "print" in tokens and not tokens & {"screen", "all"}
        dark_only = (
            "prefers-color-scheme" in tokens and "dark" in tokens
        )
        if not (print_only or dark_only):
            return False
    return True


def _page_rule_hides(body, external_custom=()):
    """Skip-unknown parse of one page rule body; True when it evaluably hides.

    Shares inline-style semantics: unknown properties are ignored (a margin
    reset must not hide a page); undecodable declarations and per-body caps
    over-hide because they could conceal a display:none.
    """
    parsed = _parse_inline_style(body)
    if parsed is None:
        return True
    standard, custom = parsed
    return _declarations_hide(standard, custom, external_custom)


def _add_selector_hide_targets(part, classes, ids, tags):
    """Over-hide the SUBJECT compound one hiding selector part names.

    Every element the real rule hides matches the rightmost compound, so
    hiding all subject matches is a superset; ancestor/sibling context must
    never become a target — root-level skin/theme classes appear as context
    in ordinary hide rules and hiding them blanks the page (Wikipedia's
    .skin--responsive, Blockworks' .dark-mode). :not(...) is deleted first —
    its matches are a subset of the remainder, so keeping it would invert
    over-hide into under-hide. Attribute and pseudo segments are spaced out
    before the combinator split; any surviving fragment of the true subject
    still matches a superset of it. Parts with no extractable component
    (bare *, attribute-only shapes like the [hidden] reset idiom) are
    residual; the native attribute checks in _VisibleTextParser already
    cover [hidden].
    """
    part = _CSS_NOT_SEGMENT_RE.sub(" ", part)
    part = _CSS_ATTR_SEGMENT_RE.sub(" ", part)
    part = _CSS_PSEUDO_SEGMENT_RE.sub(" ", part)
    compounds = [
        compound
        for compound in re.split(r"[\s>+~]+", part)
        if compound
    ]
    if not compounds:
        return
    subject = compounds[-1]
    found_classes = _CSS_CLASS_TOKEN_RE.findall(subject)
    found_ids = _CSS_ID_TOKEN_RE.findall(subject)
    if found_classes or found_ids:
        classes.update(found_classes)
        ids.update(found_ids)
        return
    tags.update(
        token.casefold() for token in _CSS_TAG_TOKEN_RE.findall(subject)
    )


def _collect_hide_rules(css, classes, ids, tags, budget, depth=0, external_custom=()):
    """Walk one CSS level, over-applying conditional blocks; returns budget.

    None means the rule budget ran out — the remainder is residual and the
    already-collected targets still apply.
    """
    if depth > _MAX_PAGE_STYLE_AT_DEPTH:
        return budget
    for prelude, body in _css_scan(css):
        if body is None:
            continue
        if budget is None or budget <= 0:
            return None
        stripped_prelude = prelude.strip()
        if stripped_prelude.startswith("@"):
            match = _CSS_AT_RULE_NAME_RE.match(stripped_prelude)
            name = match.group(1).casefold() if match else ""
            if name in _NON_ELEMENT_AT_RULES:
                continue
            if name == "media" and _media_never_canonical(stripped_prelude):
                continue
            budget = _collect_hide_rules(
                body, classes, ids, tags, budget, depth + 1, external_custom
            )
            continue
        budget -= 1
        if not _page_rule_hides(body, external_custom):
            continue
        for part in prelude.split(","):
            _add_selector_hide_targets(part, classes, ids, tags)
    return budget


def _collect_css_custom_properties(css, names, budget, depth=0):
    """Collect possible inherited definitions within the existing CSS work caps."""
    if depth > _MAX_PAGE_STYLE_AT_DEPTH:
        return budget
    for prelude, body in _css_scan(css):
        if body is None:
            continue
        if budget <= 0:
            break
        budget -= 1
        if prelude.strip().startswith("@"):
            budget = _collect_css_custom_properties(body, names, budget, depth + 1)
        else:
            parsed = _parse_inline_style(body)
            if parsed is not None:
                names.update(parsed[1])
    return budget


def _page_visibility_rules(decoded, *, custom_properties=None):
    """Best-effort hide targets from evaluable inline page CSS (never raises).

    Posture (spec 2026-08-12): unevaluated CSS — external stylesheets,
    @import, tokenizer-abandoned or past-work-bound remainders — no longer
    rejects the page; its hiding is accepted residual risk. Evaluation
    over-hides on ambiguity, which can only lose visible text, never reveal
    hidden text.
    """
    scanner = _PageStyleScanner()
    scanner.feed(decoded)
    scanner.close()
    css = _strip_page_css_comments("".join(scanner.parts))
    if custom_properties is None:
        custom_properties = set()
    custom_properties.update(scanner.custom_properties)
    _collect_css_custom_properties(css, custom_properties, _MAX_PAGE_STYLE_RULES)
    hidden_classes = set()
    hidden_ids = set()
    hidden_tags = set()
    _collect_hide_rules(
        css, hidden_classes, hidden_ids, hidden_tags, _MAX_PAGE_STYLE_RULES,
        external_custom=custom_properties,
    )
    return (
        frozenset(hidden_classes),
        frozenset(hidden_ids),
        frozenset(hidden_tags),
    )


class _VisibleTextParser(HTMLParser):
    """Collect rendered text while suppressing executable and hidden markup."""

    _HIDDEN_TAGS = {
        "head", "noscript", "script", "style", "svg", "template", "title",
    }
    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
    _BREAK_TAGS = {
        "article", "blockquote", "br", "div", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "hr", "li", "main", "p", "section", "tr",
    }

    def __init__(self, *, hidden_classes=(), hidden_ids=(), hidden_tags=(),
                 custom_properties=(), content_only=False, exclude_boilerplate=False):
        super().__init__(convert_charrefs=True)
        self._suppressed = []
        self.parts = []
        self.hidden_classes = frozenset(hidden_classes)
        self.hidden_ids = frozenset(hidden_ids)
        self.hidden_tags = frozenset(hidden_tags)
        self.custom_properties = frozenset(custom_properties)
        self.content_only = content_only
        self.exclude_boilerplate = exclude_boilerplate
        self.has_content_root = False
        self.has_visible_password = False

    def _is_hidden(self, tag, values, attrs):
        # Hide-relevant attributes consider EVERY duplicate occurrence: if any
        # occurrence hides, the element hides (spec 2026-08-12 over-hide union).
        return (
            tag in self._HIDDEN_TAGS
            or tag in self.hidden_tags
            or "hidden" in values
            or any(
                value.strip().casefold() == "true"
                for value in _attr_occurrences(attrs, "aria-hidden")
            )
            or "inert" in values
            or (tag == "dialog" and "open" not in values)
            or any(
                value in self.hidden_ids
                for value in _attr_occurrences(attrs, "id")
            )
            or any(
                token in self.hidden_classes
                for value in _attr_occurrences(attrs, "class")
                for token in value.split()
            )
            or any(
                _inline_style_is_hidden(value, self.custom_properties)
                for value in _attr_occurrences(attrs, "style")
            )
        )

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"article", "main"}:
            self.has_content_root = True
        values = _html_attribute_map(attrs)
        parent_hidden = self._suppressed[-1][1] if self._suppressed else False
        parent_closed_details = (
            self._suppressed[-1][2] if self._suppressed else False
        )
        parent_closed_content = (
            self._suppressed[-1][3] if self._suppressed else False
        )
        closed_content = parent_closed_content or (
            parent_closed_details and tag != "summary"
        )
        hidden = (
            parent_hidden
            or closed_content
            or self._is_hidden(tag, values, attrs)
        )
        if tag == "input" and values.get("type", "").lower() == "password" and not hidden:
            self.has_visible_password = True
        if tag in self._BREAK_TAGS and not hidden:
            self.parts.append("\n")
        if tag not in self._VOID_TAGS:
            self._suppressed.append((
                tag,
                hidden,
                tag == "details" and "open" not in values,
                closed_content,
            ))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        matched = next((
            index
            for index in range(len(self._suppressed) - 1, -1, -1)
            if self._suppressed[index][0] == tag
        ), None)
        if matched is None:
            return
        hidden = self._suppressed[matched][1]
        del self._suppressed[matched:]
        if tag in self._BREAK_TAGS and not hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.content_only and not any(row[0] in {"article", "main"} for row in self._suppressed):
            return
        if self.exclude_boilerplate and any(
                row[0] in {"nav", "footer", "aside", "header"} for row in self._suppressed):
            return
        if not self._suppressed or not self._suppressed[-1][1]:
            self.parts.append(data)


def _collapse_visible_text(value):
    """Normalize whitespace into stable non-duplicated visible text lines."""
    lines = []
    for line in value.splitlines():
        clean = " ".join(line.split())
        if clean and (not lines or clean != lines[-1]):
            lines.append(clean)
    return "\n".join(lines)


def _decode_body(body, content_type):
    """Decode a bounded body using a strict quoted or token charset parameter."""
    charset = None
    for raw_parameter in content_type.split(";")[1:]:
        name, separator, raw_value = raw_parameter.partition("=")
        if name.strip().lower() != "charset":
            continue
        if not separator or charset is not None:
            raise GroundingFailure("malformed response charset")
        value = raw_value.strip()
        if value.startswith('"'):
            if len(value) < 2 or not value.endswith('"'):
                raise GroundingFailure("malformed response charset")
            value = value[1:-1]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise GroundingFailure("malformed response charset")
        charset = value
    charset = charset or "utf-8"
    try:
        return body.decode(charset, errors="strict")
    except LookupError as exc:
        raise GroundingFailure("unsupported response charset") from exc
    except UnicodeDecodeError as exc:
        raise GroundingFailure("response text decoding failed") from exc


def _extract_xml_text(decoded):
    """Extract XML text nodes without allowing DTD or entity declarations."""
    if _XML_DECLARATION_RE.search(decoded):
        raise GroundingFailure(
            "XML DTD or entity declarations are forbidden"
        )
    try:
        root = ElementTree.fromstring(decoded)
    except ElementTree.ParseError as exc:
        raise GroundingFailure("malformed XML response") from exc

    # ElementTree ignores comments and processing instructions by default. Text
    # and tails preserve human-readable feed content without exposing markup.
    parts = []
    for element in root.iter():
        if element.text:
            parts.append(element.text)
        if element.tail:
            parts.append(element.tail)
    return _collapse_visible_text("\n".join(parts))[:8_000]


def _extract_visible_text(body, content_type, *, content_only=False, exclude_boilerplate=False):
    """Return bounded text, excluding non-visible HTML content."""
    decoded = _decode_body(body, content_type)
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime in _XML_CONTENT_TYPES:
        return _extract_xml_text(decoded)
    if mime not in {"text/html", "application/xhtml+xml"}:
        return _collapse_visible_text(decoded)[:8_000]
    custom_properties = set()
    hidden_classes, hidden_ids, hidden_tags = _page_visibility_rules(
        decoded, custom_properties=custom_properties
    )
    parser = _VisibleTextParser(
        hidden_classes=hidden_classes,
        hidden_ids=hidden_ids,
        hidden_tags=hidden_tags,
        custom_properties=custom_properties,
        content_only=content_only,
        exclude_boilerplate=exclude_boilerplate,
    )
    parser.feed(decoded)
    parser.close()
    if parser.has_visible_password:
        raise GroundingFailure("source requires sign-in")
    visible = _collapse_visible_text("".join(parser.parts))
    return visible[:8_000]


def _validated_url(value):
    """Canonicalize a credential-free URL on the standard HTTP(S) port."""
    if not isinstance(value, str) or not value or len(value) > 2_048:
        raise GroundingFailure("invalid URL length")
    if any(
        ord(char) < 32 or ord(char) == 127 or char.isspace()
        for char in value
    ):
        raise GroundingFailure("URL contains whitespace or control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise GroundingFailure("URL authority is malformed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise GroundingFailure("URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise GroundingFailure("URL userinfo is forbidden")
    host = parsed.hostname.rstrip(".").lower()
    if not host:
        raise GroundingFailure("URL hostname is malformed")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise GroundingFailure("URL hostname is malformed") from exc
    expected_port = 443 if scheme == "https" else 80
    port = expected_port if port is None else port
    if port != expected_port:
        raise GroundingFailure("URL port is forbidden")
    host_display = f"[{host}]" if ":" in host else host
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query_pairs = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not (
            key.lower().startswith("utm_")
            or key.lower() in {"fbclid", "gclid", "mc_cid", "mc_eid"}
        )
    ]
    query = quote(
        urlencode(query_pairs, doseq=True),
        safe="=&?/:;+,%@-._~",
    )
    netloc = host_display
    canonical = urlunsplit((scheme, netloc, path, query, ""))
    target = path + (f"?{query}" if query else "")
    return canonical, scheme, host, port, target


def parse_youtube_video_url(value):
    """Bind one supported public YouTube URL to its exact video identity."""
    if not isinstance(value, str):
        return None
    try:
        original = urlsplit(value)
        canonical, scheme, host, _, _ = _validated_url(value)
    except (TypeError, ValueError, GroundingFailure):
        return None
    # A literal fragment delimiter is rejected even when its value is empty.
    if scheme != "https" or "#" in value or original.fragment:
        return None
    parsed = urlsplit(canonical)
    video_id = None
    if host in _YOUTUBE_WATCH_HOSTS and parsed.path == "/watch":
        values = [
            item
            for key, item in parse_qsl(
                parsed.query, keep_blank_values=True
            )
            if key == "v"
        ]
        if len(values) != 1:
            return None
        video_id = values[0]
    elif host in _YOUTUBE_WATCH_HOSTS:
        match = re.fullmatch(
            r"/shorts/([A-Za-z0-9_-]{11})/?", parsed.path
        )
        video_id = match.group(1) if match else None
    elif host in _YOUTUBE_SHORT_HOSTS:
        match = re.fullmatch(
            r"/([A-Za-z0-9_-]{11})/?", parsed.path
        )
        video_id = match.group(1) if match else None
    if (
        video_id is None
        or _YOUTUBE_VIDEO_ID_RE.fullmatch(video_id) is None
    ):
        return None
    return video_id, "https://www.youtube.com/watch?v=" + video_id


def canonical_source_url(value):
    """Canonicalize a source URL and strictly dispatch X status identities."""
    if is_private_telegram_url(value):
        return parse_telegram_message_url(value)[0]
    youtube = parse_youtube_video_url(value)
    if youtube is not None:
        return youtube[1]
    canonical, _, host, _, _ = _validated_url(value)
    if _is_youtube_host(host):
        raise GroundingFailure("unsupported YouTube URL")
    if not _is_x_host(host):
        return canonical
    parsed = urlsplit(canonical)
    match = _X_STATUS_PATH.fullmatch(parsed.path)
    if match is None:
        raise GroundingFailure("unsupported X/Twitter URL")
    return f"https://x.com/{match.group(1)}/status/{match.group(2)}"


_canonical_source_url = canonical_source_url


def is_private_telegram_url(value):
    try:
        parsed = urlsplit(value)
        return (
            (parsed.hostname or "").lower().rstrip(".")
            in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
            and parsed.path.startswith("/c/")
        )
    except (TypeError, ValueError):
        return False


def parse_telegram_message_url(value):
    """Accept exact HTTPS private-message links, optionally naming a forum topic."""
    if not is_private_telegram_url(value):
        raise TelegramMessageUnavailable("unsupported Telegram message link")
    try:
        canonical, scheme, _, _, _ = _validated_url(value)
        parsed = urlsplit(canonical)
        if scheme != "https" or "?" in value or "#" in value:
            raise ValueError("unsupported link parameters")
        match = re.fullmatch(r"/c/([1-9][0-9]{0,12})/(?:([1-9][0-9]{0,9})/)?([1-9][0-9]{0,9})/?", parsed.path)
        if match is None:
            raise ValueError("unsupported link path")
        chat_id = -(1_000_000_000_000 + int(match.group(1)))
        topic_id = int(match.group(2) or 0)
        message_id = int(match.group(3))
        if max(topic_id, message_id) > (1 << 31) - 1:
            raise ValueError("message id out of range")
        canonical = "https://t.me" + parsed.path.rstrip("/")
        return canonical, chat_id, topic_id, message_id
    except (TypeError, ValueError):
        raise TelegramMessageUnavailable("unsupported Telegram message link") from None


def _host_header(host, scheme, port):
    """Render the validated authority used by the HTTP Host header."""
    rendered = f"[{host}]" if ":" in host else host
    expected = 443 if scheme == "https" else 80
    return rendered if port == expected else f"{rendered}:{port}"


def _remaining_fetch_time(deadline):
    """Return positive time remaining on the single absolute fetch deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GroundingFailure("source fetch timed out")
    return remaining


def _transport_sockets(connection, response):
    """Return each active socket reachable from a connection or response."""
    candidates = [getattr(connection, "sock", None)]
    if response is not None:
        fp = getattr(response, "fp", None)
        raw = getattr(fp, "raw", None)
        candidates.append(getattr(raw, "_sock", None))
    sockets = []
    seen = set()
    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        sockets.append(candidate)
    return tuple(sockets)


def _reset_io_timeout(connection, response, deadline):
    """Apply the remaining absolute deadline to active transport sockets."""
    remaining = _remaining_fetch_time(deadline)
    connection.timeout = remaining
    for candidate in _transport_sockets(connection, response):
        candidate.settimeout(remaining)
    return remaining


def _abort_transport(connection, response):
    """Interrupt active socket I/O and close all per-hop transport state."""
    for candidate in _transport_sockets(connection, response):
        shutdown = getattr(candidate, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
    if response is not None:
        try:
            response.close()
        except Exception:
            pass
    try:
        connection.close()
    except Exception:
        pass


def _read_bounded_body(
        response, connection, deadline, max_bytes,
        response_byte_consumer=None):
    """Read incrementally under both the byte cap and absolute deadline."""
    chunks = []
    total = 0
    while total <= max_bytes:
        _reset_io_timeout(connection, response, deadline)
        requested = min(64 * 1024, max_bytes + 1 - total)
        chunk = response.read(requested)
        _remaining_fetch_time(deadline)
        if not isinstance(chunk, bytes):
            raise GroundingFailure("response body is malformed")
        if not chunk:
            break
        if response_byte_consumer is not None:
            response_byte_consumer(len(chunk))
        if len(chunk) > requested:
            raise GroundingFailure("response body too large")
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise GroundingFailure("response body too large")
    return b"".join(chunks)


def _validated_content_length(response, max_bytes):
    """Reject malformed or oversized declared response lengths before reading."""
    raw_value = response.getheader("content-length")
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    if not re.fullmatch(r"[0-9]+", value):
        raise GroundingFailure("malformed Content-Length")
    try:
        declared = int(value)
    except ValueError as exc:
        raise GroundingFailure("malformed Content-Length") from exc
    if declared > max_bytes:
        raise GroundingFailure("response body too large")
    return declared


@dataclass(frozen=True)
class HttpResource:
    """Bytes obtained through the same pinned, bounded transport as evidence."""

    canonical_url: str
    body: bytes
    content_type: str


class SafeHttpFetcher:
    """Fetch text through an injected, DNS-pinned, bounded HTTP transport."""

    def __init__(
        self,
        *,
        limits=None,
        resolver=socket.getaddrinfo,
        connection_factory=_default_connection_factory,
        response_byte_consumer=None,
        renderer=None,
        resource_request_consumer=None,
    ):
        self.limits = limits or GroundingLimits()
        self.resolver = resolver
        self.connection_factory = connection_factory
        self.response_byte_consumer = response_byte_consumer
        self.renderer = renderer
        self.resource_request_consumer = resource_request_consumer

    def fetch(self, url, *, max_response_bytes=None):
        deadline = min(
            time.monotonic() + self.limits.fetch_timeout,
            getattr(self, "collection_deadline", float("inf")),
        )
        budget = min(self.limits.max_source_bytes, max_response_bytes or self.limits.max_source_bytes)
        consumed = 0
        previous_consumer = self.response_byte_consumer
        consumption_lock = threading.Lock()

        def consume(amount):
            nonlocal consumed
            # Keep this capture's debit independent of a later source even
            # when an aborted transport finishes draining asynchronously.
            with consumption_lock:
                consumed += amount
                if previous_consumer is not None:
                    previous_consumer(amount)

        self.response_byte_consumer = consume
        try:
            resource = self.fetch_resource(
                url, max_response_bytes=budget if max_response_bytes is None else max_response_bytes,
                deadline=deadline,
            )
            decoded = _decode_body(resource.body, resource.content_type)
            projected = compact_machine_source_text(resource.canonical_url, decoded)
            text = projected if projected != decoded else _extract_visible_text(resource.body, resource.content_type)
            reader = "http"
            truncated = len(text) >= 8_000
            mime = resource.content_type.split(";", 1)[0].strip().lower()
            needs_render = not text
            if mime in {"text/html", "application/xhtml+xml"}:
                has_content_root = re.search(r"<(?:article|main)(?:\s|>)", decoded, re.I) is not None
                if has_content_root:
                    content = _extract_visible_text(resource.body, resource.content_type, content_only=True)
                    # Navigation does not prove the requested article loaded.
                    # Keep article text ahead of header/footer boilerplate.
                    if content:
                        text = content
                    needs_render = not content or (len(content) < 160 and "<script" in decoded.lower())
                else:
                    text = _extract_visible_text(resource.body, resource.content_type, exclude_boilerplate=True)
                    needs_render = not text or (len(text) < 160 and "<script" in decoded.lower())
            if (self.renderer is not None and mime in {"text/html", "application/xhtml+xml"}
                    and needs_render):
                def fetch_resource(resource_url, *, accepted_content_types):
                    if self.resource_request_consumer is not None:
                        self.resource_request_consumer()
                    with consumption_lock:
                        remaining = budget - consumed
                    if remaining <= 0:
                        raise GroundingFailure("source byte budget exceeded")
                    return self.fetch_resource(
                        resource_url, max_response_bytes=remaining,
                        accepted_content_types=accepted_content_types, deadline=deadline,
                    )

                rendered = self.renderer.render(
                    resource, fetch_resource=fetch_resource, deadline=deadline,
                    max_response_bytes=budget,
                )
                text, reader, truncated = rendered.text, "browser", rendered.truncated
            elif reader == "http":
                truncated = len(text.encode("utf-8")) >= 8_000
            if not text:
                raise GroundingFailure("response contains no visible text")
            _remaining_fetch_time(deadline)
            return FetchedSource(
                canonical_url=resource.canonical_url,
                source_ref="web:" + hashlib.sha256(resource.canonical_url.encode()).hexdigest()[:20],
                text=text, response_bytes=consumed, reader=reader, truncated=truncated,
            )
        finally:
            self.response_byte_consumer = previous_consumer

    def fetch_resource(self, url, *, max_response_bytes=None,
                       accepted_content_types=None, deadline=None):
        """Fetch public bytes without granting browser requests any extra authority."""
        response_limit = self.limits.max_response_bytes
        if max_response_bytes is not None:
            if type(max_response_bytes) is not int or max_response_bytes <= 0:
                raise GroundingFailure("invalid response byte limit")
            response_limit = min(response_limit, max_response_bytes)
        current = url
        redirect_from_scheme = None
        deadline = min(time.monotonic() + self.limits.fetch_timeout,
                       float("inf") if deadline is None else deadline)
        collection_deadline = getattr(self, "collection_deadline", None)
        if collection_deadline is not None:
            deadline = min(deadline, float(collection_deadline))
        for redirect_count in range(self.limits.max_redirects + 1):
            canonical, scheme, host, port, target = _validated_url(current)
            if is_private_telegram_url(canonical):
                raise TelegramMessageUnavailable("Telegram message needs authenticated lookup")
            if _is_youtube_host(host):
                raise GroundingFailure(
                    "YouTube URLs require the canonical caption fetcher"
                )
            if _is_x_host(host):
                raise GroundingFailure(
                    "X/Twitter URLs require the canonical X fetcher"
                )
            remaining = _remaining_fetch_time(deadline)
            addresses = _resolve_before_deadline(
                host, port, self.resolver, remaining
            )
            remaining = _remaining_fetch_time(deadline)
            if redirect_from_scheme == "https" and scheme == "http":
                raise GroundingFailure("HTTPS downgrade redirect rejected")
            connection = self.connection_factory(
                scheme,
                host,
                addresses[0],
                port,
                min(float(self.limits.fetch_timeout), remaining),
            )
            response = None
            timed_out = threading.Event()
            watchdog_state = {"response": None}

            def abort_at_deadline(
                connection=connection,
                timed_out=timed_out,
                watchdog_state=watchdog_state,
            ):
                timed_out.set()
                _abort_transport(
                    connection, watchdog_state["response"]
                )

            watchdog = threading.Timer(
                _remaining_fetch_time(deadline), abort_at_deadline
            )
            watchdog.daemon = True
            watchdog.start()
            try:
                _reset_io_timeout(connection, response, deadline)
                connection.request("GET", target, headers={
                    "Host": _host_header(host, scheme, port),
                    "User-Agent": "BenthicGrounding/1.0",
                    "Accept": (
                        "text/html, application/xhtml+xml, application/json, "
                        "application/rss+xml, application/atom+xml, "
                        "application/xml, text/xml, text/markdown;q=0.9, text/plain;q=0.9"
                    ),
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                _remaining_fetch_time(deadline)
                _reset_io_timeout(connection, response, deadline)
                response = connection.getresponse()
                watchdog_state["response"] = response
                _remaining_fetch_time(deadline)
                status = int(response.status)
                if status in _REDIRECT_STATUSES:
                    if redirect_count >= self.limits.max_redirects:
                        raise GroundingFailure("too many redirects")
                    location = response.getheader("location")
                    if not location:
                        raise GroundingFailure("redirect has no location")
                    candidate = urljoin(canonical, location)
                    redirect_from_scheme = scheme
                    current = candidate
                    continue
                if status < 200 or status >= 300:
                    raise GroundingFailure(f"HTTP status {status}")
                encoding = str(
                    response.getheader("content-encoding", "identity")
                ).lower()
                if encoding not in {"", "identity"}:
                    raise GroundingFailure("encoded response rejected")
                content_type = str(response.getheader("content-type", ""))
                mime = content_type.split(";", 1)[0].strip().lower()
                if mime not in (_TEXT_CONTENT_TYPES if accepted_content_types is None else accepted_content_types):
                    raise GroundingFailure("unsafe content type")
                _validated_content_length(
                    response, response_limit
                )
                body = _read_bounded_body(
                    response,
                    connection,
                    deadline,
                    response_limit,
                    self.response_byte_consumer,
                )
                return HttpResource(canonical, body, content_type)
            except GroundingFailure:
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                if timed_out.is_set() or time.monotonic() >= deadline:
                    raise GroundingFailure("source fetch timed out") from exc
                raise GroundingFailure("source transport failed") from exc
            finally:
                watchdog.cancel()
                if response is not None:
                    response.close()
                connection.close()
        raise GroundingFailure("too many redirects")


def parse_x_status_url(url):
    if not isinstance(url, str):
        return None
    try:
        canonical = canonical_source_url(url)
    except (TypeError, ValueError, GroundingFailure):
        return None
    parsed = urlsplit(canonical)
    if parsed.hostname != "x.com":
        return None
    match = _X_STATUS_PATH.fullmatch(parsed.path)
    return (match.group(1), match.group(2)) if match else None


def parse_quoted_tweet(value, parent_url, parent_status_id):
    """Accept only quote data nested under the already ID-matched focal tweet."""
    if not isinstance(value, dict):
        return ()
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        return ()
    author_value = value.get("author")
    if isinstance(author_value, dict):
        author_value = author_value.get("username")
    author = str(author_value or "")[:64] or None
    quote_id = str(value.get("id") or "")
    if quote_id and not quote_id.isdigit():
        raise GroundingFailure("quoted tweet id is malformed")
    source_ref = (
        f"x:{quote_id}" if quote_id
        else f"x:{parent_status_id}:quote"
    )
    canonical_url = (
        f"https://x.com/{author}/status/{quote_id}"
        if quote_id and author
        else parent_url
    )
    return (FetchedSource(
        canonical_url=canonical_url,
        source_ref=source_ref,
        text=text.strip()[:8_000],
        author=author,
        timestamp=str(value.get("created_at") or "")[:64] or None,
    ),)


def _close_twitter_pipe(stream):
    """Close one owned child pipe inside a bounded cleanup context."""
    if stream is None:
        return
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _defer_source_process_reap(
        process, readers, streams, process_slots):
    """Reap one killed source child before returning its owned slot."""
    def reap():
        reaped = False
        try:
            process.wait()
            reaped = True
        except ChildProcessError:
            # Another waiter already collected the child, so capacity is safe
            # to return even though this waiter had nothing left to reap.
            reaped = True
        except Exception:
            pass
        finally:
            try:
                for stream in streams:
                    _close_twitter_pipe(stream)
                for reader in readers:
                    try:
                        reader.join()
                    except RuntimeError:
                        # A sibling reader may not have started when thread
                        # creation fails partway through the launch loop.
                        pass
            finally:
                if reaped:
                    process_slots.release()

    worker = threading.Thread(target=reap, daemon=True)
    try:
        worker.start()
    except Exception:
        # Keep the already-acquired slot quarantined if no reaper can start.
        return False
    return True


def _defer_twitter_reap(process, readers, streams):
    """Retain the Twitter-specific cleanup interface for existing callers."""
    return _defer_source_process_reap(
        process, readers, streams, _TWITTER_PROCESS_SLOTS
    )


def _run_bounded_source_process(
        argv, deadline, max_stdout_bytes, *, process_slots,
        source_label, response_byte_consumer=None, env=None):
    """Run one trusted source helper with bounded pipes and confirmed reaping."""
    if type(max_stdout_bytes) is not int or max_stdout_bytes <= 0:
        raise GroundingFailure("invalid source output byte limit")
    if (
        not isinstance(source_label, str)
        or not source_label
        or len(source_label) > 64
        or any(not char.isprintable() for char in source_label)
    ):
        raise GroundingFailure("invalid source process label")

    def remaining_time():
        return max(0.0, float(deadline) - time.monotonic())

    if remaining_time() <= 0:
        raise GroundingFailure(f"{source_label} timed out")
    acquired = process_slots.acquire(timeout=remaining_time())
    if not acquired:
        raise GroundingFailure(f"{source_label} process slots saturated")
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **({"env": env, "stdin": subprocess.DEVNULL} if env is not None else {}),
        )
    except Exception as exc:
        process_slots.release()
        raise GroundingFailure(f"{source_label} failed to start") from exc

    stdout_chunks = []
    stderr_chunks = []
    stdout_size = 0
    stderr_size = 0
    stdout_overflow = threading.Event()
    reader_errors = []
    byte_errors = []

    def stop_process():
        try:
            process.terminate()
        except OSError:
            pass

    def drain(stream, chunks, limit, *, stdout):
        nonlocal stdout_size, stderr_size
        try:
            while True:
                if remaining_time() <= 0:
                    return
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                if not isinstance(chunk, bytes):
                    reader_errors.append("malformed subprocess output")
                    stop_process()
                    return
                if stdout and response_byte_consumer is not None:
                    try:
                        response_byte_consumer(len(chunk))
                    except GroundingFailure as exc:
                        byte_errors.append(exc)
                        stop_process()
                        return
                    except Exception:
                        byte_errors.append(GroundingFailure(
                            f"{source_label} response byte accounting failed"
                        ))
                        stop_process()
                        return
                size = stdout_size if stdout else stderr_size
                remaining = max(0, limit - size)
                if remaining:
                    chunks.append(chunk[:remaining])
                size += len(chunk)
                if stdout:
                    stdout_size = size
                    if size > limit:
                        stdout_overflow.set()
                        stop_process()
                else:
                    stderr_size = size
        except Exception:
            reader_errors.append("subprocess output drain failed")
            stop_process()

    readers = (
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout_chunks, max_stdout_bytes),
            kwargs={"stdout": True},
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr_chunks, _MAX_TWITTER_STDERR_BYTES),
            kwargs={"stdout": False},
            daemon=True,
        ),
    )
    returncode = None
    timed_out = False
    slot_deferred = False
    try:
        for reader in readers:
            reader.start()
        remaining = remaining_time()
        if remaining <= 0:
            timed_out = True
        else:
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
        if timed_out:
            try:
                process.kill()
            except OSError:
                pass
            try:
                returncode = process.wait(timeout=0)
            except subprocess.TimeoutExpired:
                slot_deferred = True
                _defer_source_process_reap(
                    process,
                    readers,
                    (process.stdout, process.stderr),
                    process_slots,
                )
        for reader in readers:
            remaining = remaining_time()
            if remaining <= 0:
                break
            reader.join(timeout=remaining)
        if timed_out or returncode is None:
            raise GroundingFailure(f"{source_label} timed out")
        if byte_errors:
            raise byte_errors[0]
        if any(reader.is_alive() for reader in readers) or reader_errors:
            raise GroundingFailure(f"{source_label} output is malformed")
        if stdout_overflow.is_set():
            raise GroundingFailure(f"{source_label} output too large")
        if returncode:
            raise GroundingFailure(f"{source_label} failed")
        try:
            return b"".join(stdout_chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GroundingFailure(
                f"{source_label} output is malformed"
            ) from exc
    finally:
        if not slot_deferred:
            if returncode is None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    returncode = process.wait(timeout=0)
                except subprocess.TimeoutExpired:
                    slot_deferred = True
                    _defer_source_process_reap(
                        process,
                        readers,
                        (process.stdout, process.stderr),
                        process_slots,
                    )
            if not slot_deferred and any(
                    reader.is_alive() for reader in readers
            ):
                slot_deferred = True
                _defer_source_process_reap(
                    process,
                    readers,
                    (process.stdout, process.stderr),
                    process_slots,
                )
            if not slot_deferred:
                _close_twitter_pipe(process.stdout)
                _close_twitter_pipe(process.stderr)
                process_slots.release()


def _parse_youtube_caption_receipt(payload, video_id, *, reservation):
    """Validate the exact tagged-union receipt emitted by the caption helper."""
    if (
        not isinstance(payload, dict)
        or not isinstance(video_id, str)
        or _YOUTUBE_VIDEO_ID_RE.fullmatch(video_id) is None
        or type(reservation) is not int
        or not 1 <= reservation <= 2_097_152
    ):
        raise GroundingFailure("invalid YouTube caption receipt")
    keys = frozenset(payload)
    if keys not in {_YOUTUBE_SUCCESS_KEYS, _YOUTUBE_FAILURE_KEYS}:
        raise GroundingFailure("invalid YouTube caption receipt")
    response_bytes = payload.get("response_bytes")
    if (
        type(response_bytes) is not int
        or not 0 <= response_bytes <= reservation
    ):
        raise GroundingFailure("invalid YouTube caption receipt")
    if keys == _YOUTUBE_FAILURE_KEYS:
        if (
            payload.get("ok") is not False
            or payload.get("failure") != "captions_unavailable"
        ):
            raise GroundingFailure("invalid YouTube caption receipt")
        return payload

    if payload.get("ok") is not True or payload.get("video_id") != video_id:
        raise GroundingFailure("invalid YouTube caption receipt")
    language = payload.get("language")
    language_code = payload.get("language_code")
    if (
        not isinstance(language, str)
        or not language
        or language != language.strip()
        or len(language.encode("utf-8")) > 128
        or any(not char.isprintable() for char in language)
        or not isinstance(language_code, str)
        or _YOUTUBE_LANGUAGE_CODE_RE.fullmatch(language_code) is None
    ):
        raise GroundingFailure("invalid YouTube caption receipt")
    if (
        type(payload.get("is_generated")) is not bool
        or type(payload.get("truncated")) is not bool
    ):
        raise GroundingFailure("invalid YouTube caption receipt")
    covered = payload.get("covered_seconds")
    total = payload.get("total_seconds")
    if (
        type(covered) not in {int, float}
        or type(total) not in {int, float}
        or not math.isfinite(covered)
        or not math.isfinite(total)
        or covered < 0
        or total < 0
        or covered > total
        or (payload.get("truncated") is False and covered != total)
    ):
        raise GroundingFailure("invalid YouTube caption receipt")
    caption_text = payload.get("caption_text")
    if (
        not isinstance(caption_text, str)
        or not caption_text
        or caption_text != caption_text.strip()
        or len(caption_text.encode("utf-8")) > 47_000
        or any(
            (ord(char) < 32 and char != "\n") or ord(char) == 127
            for char in caption_text
        )
    ):
        raise GroundingFailure("invalid YouTube caption receipt")
    return payload


def _youtube_json_object(raw):
    """Parse one JSON object while rejecting duplicate member names."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65_536:
        raise GroundingFailure("invalid YouTube caption receipt")

    def strict_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise GroundingFailure("invalid YouTube caption receipt")
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=strict_object)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GroundingFailure("invalid YouTube caption receipt") from exc
    if not isinstance(payload, dict):
        raise GroundingFailure("invalid YouTube caption receipt")
    return payload


class TelegramMessageFetcher:
    """Read one exact message through the installed, trusted Telegram helper.

    The caller supplies the ingress chat/topic, never a model-selected scope.
    The helper's exact ID range and topic filter constrain the authenticated
    request; the response ID is checked again before any text is admitted.
    """

    def __init__(self, *, python, script, runner=None, timeout=15):
        self.python = python
        self.script = script
        self.runner = runner
        self.timeout = min(15.0, max(0.001, float(timeout)))
        self.collection_deadline = None
        self.response_byte_consumer = None

    def fetch(self, url, *, scope, max_response_bytes=65_536):
        canonical, chat_id, link_topic, message_id = parse_telegram_message_url(url)
        if (
            not isinstance(scope, tuple) or len(scope) != 2
            or type(scope[0]) is not int or type(scope[1]) is not int
            or scope[0] != chat_id or not 0 <= scope[1] < (1 << 31)
            or (link_topic and link_topic != scope[1])
        ):
            raise TelegramMessageUnavailable("Telegram message outside current chat or topic")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise TelegramMessageUnavailable("Telegram message byte budget exhausted")
        output_limit = min(65_536, max_response_bytes)
        deadline = time.monotonic() + self.timeout
        if self.collection_deadline is not None:
            deadline = min(deadline, self.collection_deadline)
        argv = [self.python, self.script, "messages", str(chat_id),
                "--min-id", str(message_id - 1), "--max-id", str(message_id + 1),
                "--limit", "1"]
        if scope[1]:
            argv.extend(["--topic", str(scope[1])])
        try:
            remaining = _remaining_fetch_time(deadline)
            if self.runner is None:
                raw = _run_bounded_source_process(
                    argv, deadline, output_limit,
                    process_slots=_TELEGRAM_PROCESS_SLOTS,
                    source_label="telegram message",
                    response_byte_consumer=self.response_byte_consumer,
                    env={"HOME": str(Path.home()), "PATH": os.defpath,
                         "LANG": "C.UTF-8", "PYTHONUTF8": "1"},
                )
            else:
                raw = self.runner(argv, remaining)
                if self.response_byte_consumer is not None:
                    self.response_byte_consumer(len(raw.encode("utf-8")))
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > output_limit:
                raise ValueError("invalid helper output")
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise ValueError("exact message missing")
            row = rows[0]
            text = row.get("text")
            timestamp = canonical_event_time(row.get("date"))
            if (
                type(row.get("id")) is not int or row["id"] != message_id
                or not isinstance(text, str) or not text.strip()
                or len(text.encode("utf-8")) > 8_000
                or type(row.get("sender_id")) is not int
                or not row["sender_id"] or timestamp is None
            ):
                raise ValueError("invalid message receipt")
            _remaining_fetch_time(deadline)
        except (ValueError, TypeError, OSError, AttributeError):
            # Helper stderr and exception text can contain session details.
            raise TelegramMessageUnavailable("Telegram message lookup unavailable") from None
        return FetchedSource(
            canonical_url=canonical,
            source_ref="web:" + hashlib.sha256(canonical.encode()).hexdigest()[:20],
            text=text.strip(), author=str(row["sender_id"]), timestamp=timestamp,
            response_bytes=len(raw.encode("utf-8")),
        )


def resolve_telegram_fetcher(*, timeout=15):
    """Use operator-configured or installed explorer paths, never paths from message text.

    Order mirrors ln-agent.py: TELEGRAM_CLIENT_SCRIPT/TELEGRAM_CLIENT_PYTHON,
    then the repo-bundled skill with this repo's .venv, then user-level skill
    installs with their sibling .venv.
    """
    base = Path(__file__).resolve().parent
    repo_python = Path(os.environ.get("TELEGRAM_CLIENT_PYTHON", str(base / ".venv/bin/python3")))
    pairs = []
    override = os.environ.get("TELEGRAM_CLIENT_SCRIPT", "").strip()
    if override:
        pairs.append((Path(override).expanduser(), repo_python.expanduser()))
    pairs.append((base / "skills/telegram-explorer/scripts/telegram_client.py",
                  repo_python.expanduser()))
    for script in (
        Path.home() / ".agents/skills/telegram-explorer/scripts/telegram_client.py",
        Path.home() / ".codex/skills/telegram-explorer/scripts/telegram_client.py",
    ):
        pairs.append((script, script.parent / ".venv/bin/python3"))
    for script, python in pairs:
        if script.is_file() and python.is_file():
            return TelegramMessageFetcher(python=str(python), script=str(script), timeout=timeout)
    raise TelegramMessageUnavailable("Telegram message helper unavailable")


class YouTubeTranscriptFetcher:
    """Run the trusted caption helper and return exact-video evidence."""

    def __init__(
            self, *, python, script, runner=None, timeout=15,
            response_byte_consumer=None):
        if (
            not isinstance(python, str)
            or not python
            or not isinstance(script, str)
            or not script
            or type(timeout) not in {int, float}
            or not math.isfinite(timeout)
            or not 0 < timeout <= 30
        ):
            raise GroundingFailure("invalid YouTube caption fetcher")
        self.python = python
        self.script = script
        self.runner = runner
        self.timeout = float(timeout)
        self.collection_deadline = None
        self.response_byte_consumer = response_byte_consumer

    def _consume(self, amount):
        """Charge trusted network bytes to the caller's shared turn ledger."""
        if self.response_byte_consumer is None:
            return
        self.response_byte_consumer(amount)

    def _fail_unaccounted(self, reservation):
        """Spend a full reservation after an untrustworthy child outcome."""
        try:
            self._consume(reservation)
        except Exception:
            pass
        raise YouTubeTranscriptUnavailable(
            "public YouTube captions are unavailable"
        ) from None

    def fetch(self, url, *, max_response_bytes=None):
        """Fetch one supported video's public captions under fixed bounds."""
        parsed = parse_youtube_video_url(url)
        if parsed is None:
            raise YouTubeTranscriptUnavailable(
                "public YouTube captions are unavailable"
            )
        video_id, canonical = parsed
        reservation = (
            2_097_152
            if max_response_bytes is None
            else max_response_bytes
        )
        if (
            type(reservation) is not int
            or not 1 <= reservation <= 2_097_152
        ):
            raise YouTubeTranscriptUnavailable(
                "public YouTube captions are unavailable"
            )
        now = time.monotonic()
        deadline = now + self.timeout
        if self.collection_deadline is not None:
            try:
                deadline = min(deadline, float(self.collection_deadline))
            except (TypeError, ValueError, OverflowError):
                raise YouTubeTranscriptUnavailable(
                    "public YouTube captions are unavailable"
                ) from None
        remaining = deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise YouTubeTranscriptUnavailable(
                "public YouTube captions are unavailable"
            )
        argv = [
            self.python,
            self.script,
            "--video-id",
            video_id,
            "--timeout-seconds",
            f"{remaining:.6f}",
            "--max-response-bytes",
            str(reservation),
            "--max-caption-bytes",
            "47000",
        ]
        try:
            raw = (
                self.runner(argv, deadline, 65_536)
                if self.runner is not None
                else _run_bounded_source_process(
                    argv,
                    deadline,
                    65_536,
                    process_slots=_YOUTUBE_PROCESS_SLOTS,
                    source_label="youtube caption helper",
                )
            )
            payload = _parse_youtube_caption_receipt(
                _youtube_json_object(raw),
                video_id,
                reservation=reservation,
            )
        except Exception:
            self._fail_unaccounted(reservation)

        try:
            self._consume(payload["response_bytes"])
        except Exception:
            raise YouTubeTranscriptUnavailable(
                "public YouTube captions are unavailable"
            ) from None
        if payload["ok"] is not True:
            raise YouTubeTranscriptUnavailable(
                "public YouTube captions are unavailable"
            )
        header = (
            "[YouTube public captions: "
            f"language={payload['language_code']}; "
            f"generated={'true' if payload['is_generated'] else 'false'}; "
            f"truncated={'true' if payload['truncated'] else 'false'}; "
            f"covered_seconds={payload['covered_seconds']:.3f}; "
            f"total_seconds={payload['total_seconds']:.3f}]"
        )
        return FetchedSource(
            canonical_url=canonical,
            source_ref=f"youtube:{video_id}",
            text=truncate_utf8(
                header + "\n" + payload["caption_text"], 48_000
            ),
            response_bytes=payload["response_bytes"],
            evidence_byte_limit=48_000,
        )


class TwitterFocalFetcher:
    def __init__(
            self, *, python, script, cookies, runner=None, timeout=15,
            response_byte_consumer=None):
        self.python = python
        self.script = script
        self.cookies = cookies
        self.runner = runner
        self.timeout = timeout
        self.collection_deadline = None
        self.response_byte_consumer = response_byte_consumer

    @staticmethod
    def _run(
            argv, deadline, max_stdout_bytes,
            response_byte_consumer=None):
        """Drain a trusted explorer subprocess within one absolute deadline."""
        return _run_bounded_source_process(
            argv,
            deadline,
            max_stdout_bytes,
            process_slots=_TWITTER_PROCESS_SLOTS,
            source_label="twitter explorer",
            response_byte_consumer=response_byte_consumer,
        )

    def fetch(self, url, *, max_response_bytes=None):
        output_limit = _MAX_TWITTER_STDOUT_BYTES
        if max_response_bytes is not None:
            if type(max_response_bytes) is not int or max_response_bytes <= 0:
                raise GroundingFailure("invalid response byte limit")
            output_limit = min(output_limit, max_response_bytes)
        parsed = parse_x_status_url(url)
        if parsed is None:
            raise GroundingFailure("not an X status URL")
        username, status_id = parsed
        canonical = f"https://x.com/{username}/status/{status_id}"
        argv = [
            self.python, self.script,
            "--cookies", self.cookies,
            "--mode", "thread",
            "--query", canonical,
            "--max-results", "20",
        ]
        deadline = time.monotonic() + float(self.timeout)
        if self.collection_deadline is not None:
            deadline = min(deadline, float(self.collection_deadline))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GroundingFailure("twitter explorer timed out")
        if self.runner is None:
            raw = (
                self._run(
                    argv,
                    deadline,
                    output_limit,
                    self.response_byte_consumer,
                )
                if self.response_byte_consumer is not None
                else self._run(argv, deadline, output_limit)
            )
        else:
            raw = self.runner(argv, remaining)
        if not isinstance(raw, (str, bytes)):
            raise GroundingFailure("twitter explorer output is malformed")
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
        if self.runner is not None and self.response_byte_consumer is not None:
            self.response_byte_consumer(len(raw_bytes))
        if len(raw_bytes) > output_limit:
            raise GroundingFailure("twitter explorer output too large")
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GroundingFailure("twitter explorer output is malformed") from exc
        focal = json_object(raw_text).get("focal_tweet")
        if not isinstance(focal, dict) or str(focal.get("id")) != status_id:
            raise GroundingFailure("focal tweet id mismatch")
        focal_text = focal.get("text")
        if not isinstance(focal_text, str) or not focal_text.strip():
            raise GroundingFailure("focal tweet text is missing")
        author = focal.get("author") if isinstance(focal.get("author"), dict) else {}
        return FetchedSource(
            canonical_url=canonical,
            source_ref=f"x:{status_id}",
            text=focal_text.strip()[:8_000],
            author=str(author.get("username") or "")[:64] or None,
            timestamp=canonical_event_time(focal.get("created_at")),
            quoted=parse_quoted_tweet(focal.get("quoted_tweet"), canonical, status_id),
            response_bytes=len(raw_bytes),
        )


def resolve_twitter_fetcher(env=None, *, timeout=15):
    """Resolve only trusted local script, interpreter, and cookie paths."""
    env = os.environ if env is None else env
    override = env.get("TWITTER_FETCH_SCRIPT", "").strip()
    candidates = [Path(override).expanduser()] if override else []
    # Repo-bundled default — a no-op stub unless you provide your own
    # implementation (see README: Twitter/X Integration).
    candidates.append(Path(__file__).resolve().parent / "scripts/twitter_fetch.py")
    script = next((path.resolve() for path in candidates if path.is_file()), None)
    python = Path(env.get("TWITTER_FETCH_PYTHON", sys.executable)).expanduser()
    cookies = Path(env.get(
        "TWITTER_COOKIES_FILE",
        str(Path.home() / ".claude/twitter-cookies.txt"),
    )).expanduser()
    if script is None or not python.is_file() or not cookies.is_file():
        raise GroundingFailure("twitter explorer is unavailable")
    return TwitterFocalFetcher(
        python=str(python.resolve()),
        script=str(script),
        cookies=str(cookies.resolve()),
        timeout=timeout,
    )


def json_object(raw: str) -> dict:
    """Decode exactly one JSON object while rejecting duplicate object keys."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise GroundingFailure("response JSON contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(
            (raw or "").strip(), object_pairs_hook=unique_object
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise GroundingFailure("response is not strict JSON") from exc
    if not isinstance(value, dict):
        raise GroundingFailure("response JSON must be an object")
    return value


def parse_media_observations(raw, expected_count):
    """Parse exact, bounded observation rows for the selected image manifest."""
    obj = json_object(raw)
    if set(obj) != {"items"}:
        raise GroundingFailure("invalid media response keys")
    rows = obj.get("items")
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise GroundingFailure("invalid media item count")
    parsed = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "index", "observations", "visible_text"
        }:
            raise GroundingFailure("invalid media item")
        if type(row["index"]) is not int:
            raise GroundingFailure("invalid media index")
        observations = row["observations"]
        visible_text = row["visible_text"]
        if (
            not isinstance(observations, list)
            or not isinstance(visible_text, list)
            or not all(isinstance(value, str) for value in observations)
            or not all(isinstance(value, str) for value in visible_text)
        ):
            raise GroundingFailure("invalid media observations")
        parsed.append(MediaObservation(
            index=row["index"],
            observations=tuple(value[:500] for value in observations[:20]),
            visible_text=tuple(value[:500] for value in visible_text[:20]),
        ))
    if sorted(item.index for item in parsed) != list(range(expected_count)):
        raise GroundingFailure("media indices are not exact")
    return tuple(sorted(parsed, key=lambda item: item.index))


def parse_composed_reply(raw: str, evidence: EvidenceBundle) -> ComposedReply:
    """Parse a reply only when every claim references known unique evidence."""
    obj = json_object(raw)
    if set(obj) != {"decision", "reply", "claims"}:
        raise GroundingFailure("composition has invalid keys")
    decision = obj.get("decision")
    reply = obj.get("reply")
    rows = obj.get("claims")
    if (
        not isinstance(decision, str)
        or decision not in {"reply", "skip", "uncertain"}
    ):
        raise GroundingFailure("invalid decision")
    if not isinstance(reply, str) or len(reply) > 12_000:
        raise GroundingFailure("invalid reply")
    if decision == "reply" and not reply.strip():
        raise GroundingFailure("reply decision requires text")
    if not isinstance(rows, list) or len(rows) > 12:
        raise GroundingFailure("invalid claims")
    if decision != "reply" and (reply.strip() or rows):
        raise GroundingFailure("non-reply decision must not contain prose")
    known = evidence.evidence_ids()
    claims = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"claim", "evidence_ids"}
            or not isinstance(row.get("claim"), str)
        ):
            raise GroundingFailure("invalid claim row")
        claim = row["claim"].strip()
        if not claim or len(claim) > _MAX_CLAIM_CHARS:
            raise GroundingFailure("invalid claim row")
        ids = row.get("evidence_ids")
        if not isinstance(ids, list) or not ids or not all(
            isinstance(value, str) for value in ids
        ):
            raise GroundingFailure("invalid evidence ids")
        if len(ids) != len(set(ids)) or any(value not in known for value in ids):
            raise GroundingFailure("unknown or duplicate evidence id")
        claims.append(GroundedClaim(claim, tuple(ids)))
    return ComposedReply(decision, reply.strip(), tuple(claims))


def parse_verification(raw: str) -> VerificationVerdict:
    """Parse a verifier verdict and reject internally inconsistent results."""
    obj = json_object(raw)
    if set(obj) != {"pass", "unsupported_claims", "reason"}:
        raise GroundingFailure("verifier response has invalid keys")
    passed = obj.get("pass")
    unsupported = obj.get("unsupported_claims")
    reason = obj.get("reason")
    if not isinstance(passed, bool):
        raise GroundingFailure("invalid verifier pass value")
    if not isinstance(unsupported, list) or not all(
        isinstance(value, str) for value in unsupported
    ):
        raise GroundingFailure("invalid unsupported claims")
    unsupported = tuple(value.strip() for value in unsupported)
    if (
        len(unsupported) > 12
        or any(not value or len(value) > 500 for value in unsupported)
        or len(unsupported) != len(set(unsupported))
    ):
        raise GroundingFailure("invalid unsupported claims")
    if not isinstance(reason, str) or len(reason) > 2_000:
        raise GroundingFailure("invalid verifier reason")
    if passed and unsupported:
        raise GroundingFailure("passing verdict contains unsupported claims")
    # A true but non-responsive answer can fail without an unsupported fact.
    # This remains a veto, never publication approval; repair is reverified.
    if not passed and not unsupported and not reason.strip():
        raise GroundingFailure("failing verdict must identify an objection")
    return VerificationVerdict(
        passed,
        unsupported,
        reason,
    )


def public_grounding_protocol_leaks(text: str) -> tuple[str, ...]:
    """Return internal grounding phrases that must never reach a user."""
    if not isinstance(text, str):
        return ()
    normalized = unicodedata.normalize("NFKD", text)
    return tuple(
        label
        for label, pattern in _PUBLIC_GROUNDING_PROTOCOL_PATTERNS
        if pattern.search(normalized)
    )


def naturalize_public_grounding_protocol(text: str) -> str:
    """Rewrite a narrow set of internal phrases without changing factual data."""
    if not isinstance(text, str) or not text:
        return text

    def substitute(value, pattern, replacement):
        def replace_match(match):
            rendered = replacement
            if match.group(0)[:1].isupper():
                rendered = rendered[:1].upper() + rendered[1:]
            return rendered
        return re.sub(pattern, replace_match, value, flags=re.I)

    natural = text
    for pattern, replacement in (
        (r"\bfrom\s+this\s+evidence\s+alone\b", "based on the data I could verify"),
        (r"\bfrom\s+this\s+evidence\b", "based on the data I could verify"),
        (r"\bthe\s+supplied\s+evidence\b", "the sources I checked"),
        (r"\bsupplied\s+evidence\b", "sources I checked"),
        (r"\bthe\s+supplied\b", "the checked"),
        (r"\bthe\s+verifier\b", "my checks"),
        (r"\bdoes\s+not\s+establish\b", "did not let me verify"),
    ):
        natural = substitute(natural, pattern, replacement)
    return natural


def _canonical_research_url(value):
    """Canonicalize one public-model URL without performing DNS resolution."""
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError) as exc:
        raise GroundingFailure("URL authority is malformed") from exc
    if parsed.netloc.endswith(":"):
        raise GroundingFailure("URL authority has an empty port")
    canonical, _, host, _, _ = _validated_url(value)
    if len(canonical) > 2_048:
        raise GroundingFailure("canonical URL is too long")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            "." not in host
            or len(host) > 253
            or any(
                not _PUBLIC_DNS_LABEL_RE.fullmatch(label)
                for label in host.split(".")
            )
            or host in _RESERVED_RESEARCH_HOSTS
            or any(host.endswith(suffix) for suffix in _RESERVED_RESEARCH_SUFFIXES)
        ):
            raise GroundingFailure("reserved hostname is forbidden")
    else:
        if not _is_public_address(address):
            raise GroundingFailure("explicit non-public address is forbidden")
    return canonical


def _preferred_research_candidate(url: str) -> bool:
    """Prefer exact X statuses and machine-readable shapes for transport.

    This ordering is only an availability heuristic. Every candidate still
    crosses the same canonical URL validation and trusted HTTP/X fetcher.
    """
    if parse_x_status_url(url) is not None:
        return True
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return (
        host.startswith("api.")
        or "/api/" in path
        or path.endswith((".json", ".xml", ".rss", ".atom", ".txt"))
    )


def _decoded_json_source(source: FetchedSource) -> dict | None:
    """Decode one fetched JSON object while rejecting non-finite constants."""
    def reject_constant(value):
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        value = json.loads(source.text, parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _finite_number(value) -> bool:
    """Accept finite JSON numbers or finite decimal strings, never booleans."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, str) and value.strip():
        try:
            return math.isfinite(float(value))
        except ValueError:
            return False
    return False


def _gecko_market_metric_count(attributes: Mapping) -> int:
    """Count recognized finite market fields in GeckoTerminal attributes."""
    count = sum(
        _finite_number(attributes.get(key))
        for key in (
            "price_usd",
            "fdv_usd",
            "market_cap_usd",
            "reserve_in_usd",
            "total_reserve_in_usd",
        )
    )
    volume = attributes.get("volume_usd")
    if isinstance(volume, dict) and any(_finite_number(item) for item in volume.values()):
        count += 1
    return count


def compact_machine_source_text(url: str, text: str) -> str:
    """Project supported archive and pool lists into bounded evidence JSON.

    The transport byte ledger still charges the complete response. Projection
    removes bulky fields before the evidence-item cap can cut JSON mid-object.
    Archive periods are checked here; token binding is rechecked by its adapter.
    """
    if not isinstance(text, str):
        return text
    try:
        archive = project_archive(url, text)
    except (TypeError, ValueError, KeyError) as exc:
        raise GroundingFailure("invalid LN archive evidence") from exc
    if archive is not None:
        return archive
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return text
    parts = tuple(part for part in parsed.path.split("/") if part)
    lowered = tuple(part.lower() for part in parts)
    if (
        (parsed.hostname or "").lower() != "api.geckoterminal.com"
        or len(lowered) != 7
        or lowered[:3] != ("api", "v2", "networks")
        or lowered[4] != "tokens"
        or lowered[6] != "pools"
        or not _NETWORK_SLUG_RE.fullmatch(lowered[3])
        or not _EVM_ASSET_ID_RE.fullmatch(lowered[5])
    ):
        return text

    def reject_constant(value):
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        payload = json.loads(text, parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return text

    network = lowered[3]
    asset_id = lowered[5]
    projected = []
    scalar_keys = (
        "base_token_price_usd",
        "quote_token_price_usd",
        "base_token_price_native_currency",
        "quote_token_price_native_currency",
        "reserve_in_usd",
        "fdv_usd",
        "market_cap_usd",
    )
    mapping_keys = ("volume_usd", "price_change_percentage")
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "pool":
            continue
        attributes = row.get("attributes")
        if (
            not isinstance(attributes, dict)
            or not _relationship_binds_asset(row, network, asset_id)
            or _gecko_market_metric_count(attributes) < 1
        ):
            continue
        clean_attributes = {}
        name = attributes.get("name")
        if isinstance(name, str) and name.strip():
            clean_attributes["name"] = name.strip()[:256]
        for key in scalar_keys:
            value = attributes.get(key)
            if _finite_number(value):
                clean_attributes[key] = value
        for key in mapping_keys:
            value = attributes.get(key)
            if not isinstance(value, dict):
                continue
            clean_values = {
                item_key[:32]: item_value
                for item_key, item_value in list(value.items())[:16]
                if isinstance(item_key, str) and _finite_number(item_value)
            }
            if clean_values:
                clean_attributes[key] = clean_values
        created_at = attributes.get("pool_created_at")
        if isinstance(created_at, str) and created_at.strip():
            clean_attributes["pool_created_at"] = created_at.strip()[:64]

        clean_relationships = {}
        relationships = row.get("relationships")
        for key in ("base_token", "quote_token"):
            relation = (
                relationships.get(key)
                if isinstance(relationships, dict)
                else None
            )
            data = relation.get("data") if isinstance(relation, dict) else None
            identifier = data.get("id") if isinstance(data, dict) else None
            if isinstance(identifier, str) and identifier:
                clean_relationships[key] = {
                    "data": {"id": identifier[:512]},
                }
        clean_row = {
            "type": "pool",
            "attributes": clean_attributes,
            "relationships": clean_relationships,
        }
        identifier = row.get("id")
        if isinstance(identifier, str) and identifier:
            clean_row["id"] = identifier[:512]
        projected.append(clean_row)
        if len(projected) == 3:
            break
    if not projected:
        return text
    return json.dumps({"data": projected}, ensure_ascii=False, separators=(",", ":"))


def _relationship_binds_asset(row: Mapping, network: str, asset_id: str) -> bool:
    """Check GeckoTerminal token relationship IDs against one exact asset."""
    relationships = row.get("relationships")
    if not isinstance(relationships, dict):
        return False
    expected = f"{network}_{asset_id}".lower()
    for name in ("base_token", "quote_token"):
        relationship = relationships.get(name)
        data = relationship.get("data") if isinstance(relationship, dict) else None
        identifier = data.get("id") if isinstance(data, dict) else None
        if isinstance(identifier, str) and identifier.lower() == expected:
            return True
    return False


def _ohlcv_rows_valid(rows) -> bool:
    """Validate finite six-field candles with one strict timestamp direction."""
    if not isinstance(rows, list) or not rows:
        return False
    timestamps = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 6:
            return False
        if not all(_finite_number(value) for value in row):
            return False
        timestamps.append(float(row[0]))
    if len(timestamps) == 1:
        return True
    ascending = all(left < right for left, right in zip(timestamps, timestamps[1:]))
    descending = all(left > right for left, right in zip(timestamps, timestamps[1:]))
    return ascending or descending


def _machine_candidate_priority(url: str) -> int:
    """Rank recognized machine shapes before untrusted role-based fallbacks."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    parts = tuple(part.lower() for part in parsed.path.split("/") if part)
    if host == "api.geckoterminal.com":
        if len(parts) == 8 and parts[-2:] == ("ohlcv", "hour"):
            return 0
        if len(parts) == 6 and parts[4] == "tokens":
            return 1
        if len(parts) == 7 and parts[4] == "tokens" and parts[6] == "pools":
            return 2
    if (
        (host == "blockscout.com" or host.endswith(".blockscout.com"))
        and len(parts) == 4
        and parts[:3] == ("api", "v2", "tokens")
    ):
        return 3
    return 4


def _machine_source_coverage(
        candidate: ResearchCandidate,
        source: FetchedSource,
        plan: ResearchPlan) -> frozenset[ResearchRole]:
    """Return lanes proven by a recognized, contract-bound API payload."""
    del candidate
    if not plan.market_intent or plan.network is None or plan.asset_id is None:
        return frozenset()
    asset_id = plan.asset_id.lower()
    network = plan.network.lower()
    parsed = urlsplit(source.canonical_url)
    host = (parsed.hostname or "").lower()
    parts = tuple(part for part in parsed.path.split("/") if part)
    lowered = tuple(part.lower() for part in parts)
    query = parse_qs(parsed.query, keep_blank_values=True)
    payload = _decoded_json_source(source)
    if payload is None:
        return frozenset()

    if (
        (host == "blockscout.com" or host.endswith(".blockscout.com"))
        and len(lowered) == 4
        and lowered[:3] == ("api", "v2", "tokens")
        and lowered[3] == asset_id
    ):
        returned = payload.get("address_hash")
        metadata = (payload.get("name"), payload.get("symbol"), payload.get("type"))
        if (
            isinstance(returned, str)
            and returned.lower() == asset_id
            and all(isinstance(value, str) and value.strip() for value in metadata)
        ):
            return frozenset({"identity"})
        return frozenset()

    if host != "api.geckoterminal.com":
        return frozenset()
    prefix = ("api", "v2", "networks", network)
    if len(lowered) < 6 or lowered[:4] != prefix:
        return frozenset()

    data = payload.get("data")
    if (
        len(lowered) == 6
        and lowered[4] == "tokens"
        and lowered[5] == asset_id
        and isinstance(data, dict)
        and data.get("type") == "token"
    ):
        attributes = data.get("attributes")
        if not isinstance(attributes, dict):
            return frozenset()
        returned = attributes.get("address")
        metadata = (attributes.get("name"), attributes.get("symbol"))
        if (
            not isinstance(returned, str)
            or returned.lower() != asset_id
            or not all(isinstance(value, str) and value.strip() for value in metadata)
        ):
            return frozenset()
        coverage: set[ResearchRole] = {"identity"}
        if _gecko_market_metric_count(attributes) >= 2:
            coverage.add("market")
        return frozenset(coverage)

    if (
        len(lowered) == 7
        and lowered[4] == "tokens"
        and lowered[5] == asset_id
        and lowered[6] == "pools"
        and isinstance(data, list)
    ):
        for row in data:
            if not isinstance(row, dict) or row.get("type") != "pool":
                continue
            attributes = row.get("attributes")
            if (
                isinstance(attributes, dict)
                and _relationship_binds_asset(row, network, asset_id)
                and _gecko_market_metric_count(attributes) >= 1
            ):
                return frozenset({"market"})
        return frozenset()

    if (
        len(lowered) == 8
        and lowered[:5] == (*prefix, "pools")
        and lowered[6:] == ("ohlcv", "hour")
        and query.get("aggregate") == ["4"]
        and query.get("limit") == ["24"]
        and isinstance(data, dict)
        and data.get("type") == "ohlcv_request_response"
    ):
        meta = payload.get("meta")
        addresses = []
        if isinstance(meta, dict):
            for side in ("base", "quote"):
                value = meta.get(side)
                address = value.get("address") if isinstance(value, dict) else None
                if isinstance(address, str):
                    addresses.append(address.lower())
        attributes = data.get("attributes")
        rows = attributes.get("ohlcv_list") if isinstance(attributes, dict) else None
        if asset_id in addresses and _ohlcv_rows_valid(rows):
            return frozenset({"market"})
    return frozenset()


def collect_background_candidates(
        evidence: EvidenceBundle,
        urls: Sequence[str],
        url_fetcher: Callable[[str, bool], FetchedSource],
        limits: GroundingLimits,
        *,
        research_plan: ResearchPlan | None = None,
        ) -> BackgroundCollectionResult:
    """Fetch bounded candidates and retain roots adding trusted source refs.

    Failed fetches spend the existing turn ledger inside ``url_fetcher`` but
    do not consume final evidence-root capacity. A successful alias also does
    not consume capacity when its source refs are already present in focal,
    explicit-background, or earlier accepted evidence.
    """
    canonical = tuple(_canonical_research_url(url) for url in urls)
    if len(canonical) != len(set(canonical)):
        raise GroundingFailure("duplicate canonical source url")
    if len(canonical) > research_candidate_limit(evidence, limits):
        raise GroundingFailure("too many source candidates")

    if research_plan is not None and research_plan.market_intent:
        if (
            research_plan.urls != canonical
            or research_plan.network is None
            or research_plan.asset_id is None
            or not _NETWORK_SLUG_RE.fullmatch(research_plan.network)
            or not _EVM_ASSET_ID_RE.fullmatch(research_plan.asset_id)
        ):
            raise GroundingFailure("market research plan does not match candidates")
        capacity = max(
            0,
            limits.max_background_sources - len(evidence.background_source_urls),
        )
        known_refs = {
            item.source_ref
            for item in evidence.items
            if item.kind in {"focal_url", "background_url"}
        }
        accepted = []
        covered: set[ResearchRole] = set()
        accepted_machine_shapes: set[int] = set()
        attempted = 0
        fetched = {}
        failed = set()
        indexed = tuple(enumerate(research_plan.candidates))
        machine = sorted(
            (
                item for item in indexed
                if item[1].role != "thesis"
                or _machine_candidate_priority(item[1].url) < 4
            ),
            key=lambda item: (_machine_candidate_priority(item[1].url), item[0]),
        )

        def fetch_candidate(candidate):
            """Fetch each candidate at most once across the three phases."""
            nonlocal attempted
            if candidate.url in fetched:
                return fetched[candidate.url]
            if candidate.url in failed:
                return None
            attempted += 1
            try:
                source = url_fetcher(candidate.url, False)
            except GroundingFailure:
                failed.add(candidate.url)
                return None
            fetched[candidate.url] = source
            return source

        # Phase one fills only the required identity and market coverage. It
        # stops as soon as both are proven so optional roots remain available.
        for _, candidate in machine:
            if (
                len(accepted) >= capacity
                or {"identity", "market"}.issubset(covered)
            ):
                break
            source = fetch_candidate(candidate)
            if source is None:
                continue
            refs = tuple(
                _validated_source_ref(item.source_ref)
                for item in (source, *source.quoted)
            )
            if not any(ref not in known_refs for ref in refs):
                continue
            coverage = _machine_source_coverage(candidate, source, research_plan)
            new_coverage = coverage - covered
            if not new_coverage:
                continue
            known_refs.update(refs)
            accepted.append(candidate.url)
            machine_shape = _machine_candidate_priority(candidate.url)
            if machine_shape < 4:
                accepted_machine_shapes.add(machine_shape)
            covered.update(new_coverage)

        # Phase two gives one exact-contract thesis first claim on spare root
        # capacity. Adjacent social text still fails the contract-string gate.
        if {"identity", "market"}.issubset(covered):
            for _, candidate in indexed:
                if (
                    candidate.role != "thesis"
                    or _machine_candidate_priority(candidate.url) < 4
                    or len(accepted) >= capacity
                ):
                    continue
                source = fetch_candidate(candidate)
                if source is None:
                    continue
                refs = tuple(
                    _validated_source_ref(item.source_ref)
                    for item in (source, *source.quoted)
                )
                if not any(ref not in known_refs for ref in refs):
                    continue
                if research_plan.asset_id.lower() not in source.text.lower():
                    continue
                known_refs.update(refs)
                accepted.append(candidate.url)
                covered.add("thesis")
                break

        # Phase three backfills unused capacity with one source per distinct
        # validated machine shape, such as pool reserves after OHLCV. This can
        # never displace required coverage or a usable exact-token thesis.
        if {"identity", "market"}.issubset(covered):
            for _, candidate in machine:
                if len(accepted) >= capacity:
                    break
                if candidate.url in accepted:
                    continue
                source = fetch_candidate(candidate)
                if source is None:
                    continue
                refs = tuple(
                    _validated_source_ref(item.source_ref)
                    for item in (source, *source.quoted)
                )
                if not any(ref not in known_refs for ref in refs):
                    continue
                coverage = _machine_source_coverage(
                    candidate, source, research_plan
                )
                machine_shape = _machine_candidate_priority(candidate.url)
                if (
                    machine_shape >= 4
                    or "market" not in coverage
                    or machine_shape in accepted_machine_shapes
                ):
                    continue
                known_refs.update(refs)
                accepted.append(candidate.url)
                accepted_machine_shapes.add(machine_shape)
        return BackgroundCollectionResult(
            tuple(accepted),
            attempted,
            frozenset(covered),
            True,
        )

    preferred = []
    ordinary = []
    for url in canonical:
        target = preferred if _preferred_research_candidate(url) else ordinary
        target.append(url)

    capacity = max(
        0,
        limits.max_background_sources - len(evidence.background_source_urls),
    )
    known_refs = {
        item.source_ref
        for item in evidence.items
        if item.kind in {"focal_url", "background_url"}
    }
    accepted = []
    attempted = 0
    for url in (*preferred, *ordinary):
        if len(accepted) >= capacity:
            break
        attempted += 1
        try:
            source = url_fetcher(url, False)
        except GroundingFailure:
            continue
        refs = tuple(
            _validated_source_ref(item.source_ref)
            for item in (source, *source.quoted)
        )
        if not any(ref not in known_refs for ref in refs):
            continue
        known_refs.update(refs)
        accepted.append(url)
    return BackgroundCollectionResult(tuple(accepted), attempted)


def parse_research_urls(
        raw: str,
        limits: GroundingLimits,
        excluded_urls=()) -> tuple[str, ...]:
    """Return strict canonical public URLs without duplicates or silent caps."""
    obj = json_object(raw)
    if set(obj) != {"source_urls"}:
        raise GroundingFailure("research response has invalid keys")
    values = obj.get("source_urls")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise GroundingFailure("invalid source urls")
    if len(values) > limits.max_background_sources:
        raise GroundingFailure("too many source urls")
    excluded = {
        _canonical_research_url(value)
        for value in excluded_urls
    }
    canonical = tuple(_canonical_research_url(value) for value in values)
    if len(canonical) != len(set(canonical)):
        raise GroundingFailure("duplicate canonical source url")
    if any(value in excluded for value in canonical):
        raise GroundingFailure("source url duplicates excluded evidence")
    return canonical


def market_data_intent(text: str) -> bool:
    """Identify current messages that explicitly request token market data."""
    if not isinstance(text, str):
        return False
    return bool(
        _MARKET_TIMEFRAME_RE.search(text)
        or _MARKET_TERM_RE.search(text)
        or re.search(r"\b0x[0-9a-fA-F]{40}\b", text)
    )


def with_exact_gecko_token_candidate(
        plan: ResearchPlan,
        limits: GroundingLimits,
        *,
        excluded_urls=()) -> ResearchPlan:
    """Prepend one deterministic exact-token endpoint to a market plan.

    The endpoint is derived only from the already validated network and EVM
    address. At a full candidate budget it replaces one identity fallback so
    model-proposed thesis capacity and the required market role remain intact.
    """
    if (
        not isinstance(plan, ResearchPlan)
        or not plan.market_intent
        or plan.network is None
        or plan.asset_id is None
        or limits.max_background_sources <= 0
    ):
        return plan
    token_url = _canonical_research_url(
        "https://api.geckoterminal.com/api/v2/networks/"
        f"{plan.network}/tokens/{plan.asset_id}"
    )
    excluded = {
        _canonical_research_url(value)
        for value in excluded_urls
    }
    if token_url in excluded or token_url in plan.urls:
        return plan

    candidates = list(plan.candidates)
    capacity = limits.max_background_sources
    if len(candidates) >= capacity:
        removable = [
            index
            for index, candidate in enumerate(candidates)
            if candidate.role == "identity"
        ]
        if not removable:
            removable = [
                index
                for index, candidate in enumerate(candidates)
                if candidate.role != "thesis"
            ]
        if not removable:
            return plan
        candidates.pop(removable[-1])
    candidates.insert(0, ResearchCandidate(token_url, "identity"))
    return ResearchPlan(
        market_intent=True,
        network=plan.network,
        asset_id=plan.asset_id,
        candidates=tuple(candidates),
    )


def parse_research_plan(
        raw: str,
        limits: GroundingLimits,
        *,
        market_intent: bool,
        excluded_urls=()) -> ResearchPlan:
    """Parse general discovery or exact-asset market discovery fail closed."""
    if not market_intent:
        urls = parse_research_urls(raw, limits, excluded_urls=excluded_urls)
        return ResearchPlan(
            market_intent=False,
            network=None,
            asset_id=None,
            candidates=tuple(
                ResearchCandidate(url=url, role="general") for url in urls
            ),
        )

    obj = json_object(raw)
    if set(obj) != {"network", "asset_id", "sources"}:
        raise GroundingFailure("market research response has invalid keys")
    network = obj.get("network")
    asset_id = obj.get("asset_id")
    rows = obj.get("sources")
    if (
        not isinstance(network, str)
        or not _NETWORK_SLUG_RE.fullmatch(network)
    ):
        raise GroundingFailure("invalid market research network")
    if not isinstance(asset_id, str) or not _EVM_ASSET_ID_RE.fullmatch(asset_id):
        raise GroundingFailure("invalid market research asset")
    if not isinstance(rows, list) or len(rows) > limits.max_background_sources:
        raise GroundingFailure("invalid market research sources")

    excluded = {
        _canonical_research_url(value)
        for value in excluded_urls
    }
    candidates = []
    valid_roles = {"identity", "market", "thesis"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"url", "role"}:
            raise GroundingFailure("invalid market research source")
        url = row.get("url")
        role = row.get("role")
        if not isinstance(url, str):
            raise GroundingFailure("invalid market research source url")
        if not isinstance(role, str) or role not in valid_roles:
            raise GroundingFailure("invalid market research role")
        canonical = _canonical_research_url(url)
        if canonical in excluded:
            raise GroundingFailure("market source duplicates excluded evidence")
        candidates.append(ResearchCandidate(canonical, role))

    urls = tuple(candidate.url for candidate in candidates)
    if len(urls) != len(set(urls)):
        raise GroundingFailure("duplicate canonical market source url")
    roles = {candidate.role for candidate in candidates}
    if not {"identity", "market"}.issubset(roles):
        raise GroundingFailure("market research required lanes are missing")
    return ResearchPlan(
        market_intent=True,
        network=network,
        asset_id=asset_id.lower(),
        candidates=tuple(candidates),
    )


def final_disposition(*, direct: bool, failure_kind: str) -> str:
    """Fail closed for groups; direct messages receive bounded uncertainty."""
    if not direct:
        return "skip"
    return "provider_error" if failure_kind == "providers_failed" else "uncertain"
