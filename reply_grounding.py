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


_MAX_CLAIM_CHARS = 2_000
_X_ROOT_HOSTS = ("x.com", "twitter.com")
_X_STATUS_PATH = re.compile(
    r"^/([A-Za-z0-9_]{1,50})/status/([0-9]+)"
    r"(?:/(?:photo|video)/[0-9]+)?/?$"
)
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
    "text/plain",
} | _XML_CONTENT_TYPES
_XML_DECLARATION_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_MAX_INLINE_STYLE_CHARS = 4_096
_MAX_INLINE_DECLARATIONS = 64
_MAX_CSS_VAR_DEPTH = 8
_MAX_PAGE_STYLE_CHARS = 16_384
_MAX_PAGE_STYLE_RULES = 128
_CSS_CUSTOM_PROPERTY = re.compile(r"--[-_A-Za-z0-9]+")
_MAX_TWITTER_STDOUT_BYTES = 1_048_576
_MAX_TWITTER_STDERR_BYTES = 65_536
_TWITTER_PROCESS_SLOTS = threading.BoundedSemaphore(4)
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
_PUBLIC_GROUNDING_PROTOCOL_PATTERNS = (
    ("from this evidence", re.compile(r"\bfrom\s+this\s+evidence\b", re.I)),
    ("supplied evidence", re.compile(r"\b(?:the\s+)?supplied\s+evidence\b", re.I)),
    ("typed evidence", re.compile(r"\btyped\s+evidence\b", re.I)),
    ("evidence bundle", re.compile(r"\bevidence\s+bundle\b", re.I)),
    ("evidence id", re.compile(r"\bevidence[- ]ids?\b", re.I)),
    ("support matrix", re.compile(r"\bsupport[- ]matrix\b", re.I)),
    ("verification stage", re.compile(r"\bverification[- ]stage\b", re.I)),
    ("does not establish", re.compile(r"\bdoes\s+not\s+establish\b", re.I)),
    ("unsupported claim", re.compile(r"\bunsupported\s+claims?\b", re.I)),
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


class GroundingFailure(ValueError):
    """A bounded grounding contract is malformed or unsupported."""


@dataclass(frozen=True)
class GroundingLimits:
    max_background_sources: int = 3
    max_focal_urls: int = 8
    max_source_requests: int = 10
    max_source_bytes: int = 2_097_152
    # Total budget: 900s Sol, 30s provider fallback, and 30s trusted fetch.
    source_collection_timeout: int = 960
    max_evidence_bytes: int = 24_000
    fetch_timeout: int = 15
    trace_retention_days: int = 14
    photo_reference_max_age: int = 1_800
    max_response_bytes: int = 1_048_576
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


def _clean_evidence_text(value, limit):
    """Normalize and bound one evidence item's text independently."""
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"\[photo#[0-9]+\]", "[photo omitted]", value)
    value = value.replace("<", "＜").replace(">", "＞")
    value = re.sub(r"[-=]{4,}", "---", value)
    value = "".join(
        char for char in value
        if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127)
    )
    return value.strip()[:limit]


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


def _evidence_item(evidence_id, kind, text, source_ref, **kwargs):
    """Construct one normalized evidence item with a content digest."""
    # Text-document media carries a deterministic 16K excerpt plus a short
    # metadata header. Other evidence retains the established 8K item bound.
    clean = _clean_evidence_text(text, 16_512 if kind == "media" else 8_000)
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

    for kind in ("background_url", "conversation_message"):
        index = len(values) - 1
        while size() > byte_limit and index >= 0:
            if values[index].kind == kind:
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
    return "twitter" if _is_x_host(host) else "http"


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

    current_topic = int(message.get("message_thread_id") or 0)
    seen_context = {(chat_id, message_id, "incoming")}
    if isinstance(reply, dict):
        seen_context.add((chat_id, int(reply.get("message_id") or 0), "incoming"))
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
        row_topic = int(row.get("topic_id") or 0)
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
    if isinstance(reply, dict):
        reply_focal, reply_background = _classified_message_urls(
            _message_text(reply)
        )
        focal_urls.extend(reply_focal)
        explicit_background_urls.extend(reply_background)

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
    for url in focal_urls:
        source = url_fetcher(url, True)
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
                author=candidate.author,
                timestamp=canonical_event_time(candidate.timestamp),
                url=candidate.canonical_url,
            ))

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
                author=candidate.author,
                timestamp=canonical_event_time(candidate.timestamp),
                url=candidate.canonical_url,
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
    for index, item in enumerate(runtime_receipts, 1):
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
        background_source_urls=tuple(all_background_urls),
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
        if normalized_name in {"display", "visibility"}:
            _store_css_declaration(
                standard, normalized_name, decoded_value, important
            )
            continue
        return None
    return standard, custom


def _resolve_inline_css_value(value, custom, seen=(), depth=0):
    """Resolve a simple same-attribute custom property under a depth bound."""
    compact = "".join(value.split())
    match = re.fullmatch(
        r"(?i:var)\((--[-_A-Za-z0-9]+)\)", compact
    )
    if match is None:
        return None if "var(" in compact.casefold() else compact
    name = match.group(1)
    if depth >= _MAX_CSS_VAR_DEPTH or name in seen or name not in custom:
        return None
    return _resolve_inline_css_value(
        custom[name][0], custom, seen + (name,), depth + 1
    )


def _inline_style_is_hidden(value):
    """Evaluate bounded inline display and visibility declarations."""
    if not value:
        return False
    parsed = _parse_inline_style(value)
    if parsed is None:
        return True
    standard, custom = parsed
    for property_name in ("display", "visibility"):
        declaration = standard.get(property_name)
        if declaration is None:
            continue
        resolved = _resolve_inline_css_value(declaration[0], custom)
        if resolved is None:
            return True
        normalized = resolved.casefold()
        if property_name == "display" and normalized == "none":
            return True
        if property_name == "visibility" and normalized in {
            "hidden", "collapse",
        }:
            return True
    return False


def _screen_media_applies(value):
    """Return whether a bounded stylesheet media attribute can affect screen text."""
    if not value:
        return True
    media = {part.strip().casefold() for part in value.split(",") if part.strip()}
    return not media or media != {"print"}


def _html_attribute_map(attrs):
    """Build a case-insensitive map only when every HTML attribute is unique."""
    values = {}
    for key, value in attrs:
        name = str(key).casefold()
        if name in values:
            raise GroundingFailure("duplicate HTML attribute")
        values[name] = str(value or "")
    return values


class _PageStyleScanner(HTMLParser):
    """Collect bounded inline screen CSS and detect external screen stylesheets."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._capture_style = False
        self._style_depth = 0
        self.parts = []
        self.external_screen_stylesheet = False

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        values = _html_attribute_map(attrs)
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


def _page_visibility_rules(decoded):
    """Return simple class/ID selectors hidden by evaluable inline page CSS."""
    scanner = _PageStyleScanner()
    scanner.feed(decoded)
    scanner.close()
    if scanner.external_screen_stylesheet:
        raise GroundingFailure("unevaluable visibility CSS")
    css = "".join(scanner.parts)
    without_comments = _strip_css_comments(css, _MAX_PAGE_STYLE_CHARS)
    if without_comments is None:
        raise GroundingFailure("unevaluable visibility CSS")
    hidden_classes = set()
    hidden_ids = set()
    cursor = 0
    rule_count = 0
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", without_comments):
        if without_comments[cursor:match.start()].strip():
            raise GroundingFailure("unevaluable visibility CSS")
        cursor = match.end()
        rule_count += 1
        if rule_count > _MAX_PAGE_STYLE_RULES:
            raise GroundingFailure("unevaluable visibility CSS")
        parsed = _parse_inline_style(match.group(2))
        if parsed is None:
            raise GroundingFailure("unevaluable visibility CSS")
        standard, custom = parsed
        if not standard:
            continue
        hidden = False
        for property_name, declaration in standard.items():
            resolved = _resolve_inline_css_value(declaration[0], custom)
            if resolved is None:
                raise GroundingFailure("unevaluable visibility CSS")
            normalized = resolved.casefold()
            hidden = hidden or (
                property_name == "display" and normalized == "none"
            ) or (
                property_name == "visibility"
                and normalized in {"hidden", "collapse"}
            )
        selectors = [part.strip() for part in match.group(1).split(",")]
        if not selectors or any(
            re.fullmatch(r"[.#][-_A-Za-z0-9]+", selector) is None
            for selector in selectors
        ):
            raise GroundingFailure("unevaluable visibility CSS")
        if hidden:
            for selector in selectors:
                target = hidden_classes if selector.startswith(".") else hidden_ids
                target.add(selector[1:])
    if without_comments[cursor:].strip():
        raise GroundingFailure("unevaluable visibility CSS")
    return frozenset(hidden_classes), frozenset(hidden_ids)


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

    def __init__(self, *, hidden_classes=(), hidden_ids=()):
        super().__init__(convert_charrefs=True)
        self._suppressed = []
        self.parts = []
        self.hidden_classes = frozenset(hidden_classes)
        self.hidden_ids = frozenset(hidden_ids)

    def _is_hidden(self, tag, values):
        return (
            tag in self._HIDDEN_TAGS
            or "hidden" in values
            or values.get("aria-hidden", "").strip().casefold() == "true"
            or "inert" in values
            or (tag == "dialog" and "open" not in values)
            or values.get("id", "") in self.hidden_ids
            or any(
                value in self.hidden_classes
                for value in values.get("class", "").split()
            )
            or _inline_style_is_hidden(values.get("style", ""))
        )

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
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
        hidden = parent_hidden or closed_content or self._is_hidden(tag, values)
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


def _extract_visible_text(body, content_type):
    """Return bounded text, excluding non-visible HTML content."""
    decoded = _decode_body(body, content_type)
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime in _XML_CONTENT_TYPES:
        return _extract_xml_text(decoded)
    if mime not in {"text/html", "application/xhtml+xml"}:
        return _collapse_visible_text(decoded)[:8_000]
    hidden_classes, hidden_ids = _page_visibility_rules(decoded)
    parser = _VisibleTextParser(
        hidden_classes=hidden_classes,
        hidden_ids=hidden_ids,
    )
    parser.feed(decoded)
    parser.close()
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


def canonical_source_url(value):
    """Canonicalize a source URL and strictly dispatch X status identities."""
    canonical, _, host, _, _ = _validated_url(value)
    if not _is_x_host(host):
        return canonical
    parsed = urlsplit(canonical)
    match = _X_STATUS_PATH.fullmatch(parsed.path)
    if match is None:
        raise GroundingFailure("unsupported X/Twitter URL")
    return f"https://x.com/{match.group(1)}/status/{match.group(2)}"


_canonical_source_url = canonical_source_url


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


class SafeHttpFetcher:
    """Fetch text through an injected, DNS-pinned, bounded HTTP transport."""

    def __init__(
        self,
        *,
        limits=None,
        resolver=socket.getaddrinfo,
        connection_factory=_default_connection_factory,
        response_byte_consumer=None,
    ):
        self.limits = limits or GroundingLimits()
        self.resolver = resolver
        self.connection_factory = connection_factory
        self.response_byte_consumer = response_byte_consumer

    def fetch(self, url, *, max_response_bytes=None):
        response_limit = self.limits.max_response_bytes
        if max_response_bytes is not None:
            if type(max_response_bytes) is not int or max_response_bytes <= 0:
                raise GroundingFailure("invalid response byte limit")
            response_limit = min(response_limit, max_response_bytes)
        current = url
        redirect_from_scheme = None
        deadline = time.monotonic() + self.limits.fetch_timeout
        collection_deadline = getattr(self, "collection_deadline", None)
        if collection_deadline is not None:
            deadline = min(deadline, float(collection_deadline))
        for redirect_count in range(self.limits.max_redirects + 1):
            canonical, scheme, host, port, target = _validated_url(current)
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
                        "application/xml, text/xml, text/plain;q=0.9"
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
                if mime not in _TEXT_CONTENT_TYPES:
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
                decoded = _decode_body(body, content_type)
                projected = compact_machine_source_text(canonical, decoded)
                text = (
                    projected
                    if projected != decoded
                    else _extract_visible_text(body, content_type)
                )
                if not text:
                    raise GroundingFailure("response contains no visible text")
                source_hash = hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest()[:20]
                return FetchedSource(
                    canonical_url=canonical,
                    source_ref=f"web:{source_hash}",
                    text=text,
                    response_bytes=len(body),
                )
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


def _defer_twitter_reap(process, readers, streams):
    """Reap and close one child in a daemon while retaining its bounded slot."""
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
                    _TWITTER_PROCESS_SLOTS.release()

    worker = threading.Thread(target=reap, daemon=True)
    try:
        worker.start()
    except Exception:
        # Keep the already-acquired slot quarantined if no reaper can start.
        return False
    return True


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
        if type(max_stdout_bytes) is not int or max_stdout_bytes <= 0:
            raise GroundingFailure("invalid twitter output byte limit")

        def remaining_time():
            return max(0.0, float(deadline) - time.monotonic())

        if remaining_time() <= 0:
            raise GroundingFailure("twitter explorer timed out")
        acquired = _TWITTER_PROCESS_SLOTS.acquire(timeout=remaining_time())
        if not acquired:
            raise GroundingFailure("twitter explorer process slots saturated")
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:
            _TWITTER_PROCESS_SLOTS.release()
            raise GroundingFailure("twitter explorer failed to start") from exc

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
                                "twitter response byte accounting failed"
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
                    _defer_twitter_reap(
                        process, readers, (process.stdout, process.stderr)
                    )
            for reader in readers:
                remaining = remaining_time()
                if remaining <= 0:
                    break
                reader.join(timeout=remaining)
            if timed_out or returncode is None:
                raise GroundingFailure("twitter explorer timed out")
            if byte_errors:
                raise byte_errors[0]
            if any(reader.is_alive() for reader in readers) or reader_errors:
                raise GroundingFailure("twitter explorer output is malformed")
            if stdout_overflow.is_set():
                raise GroundingFailure("twitter explorer output too large")
            if returncode:
                raise GroundingFailure("twitter explorer failed")
            try:
                return b"".join(stdout_chunks).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GroundingFailure(
                    "twitter explorer output is malformed"
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
                        _defer_twitter_reap(
                            process,
                            readers,
                            (process.stdout, process.stderr),
                        )
                if not slot_deferred and any(
                        reader.is_alive() for reader in readers
                ):
                    slot_deferred = True
                    _defer_twitter_reap(
                        process, readers, (process.stdout, process.stderr)
                    )
                if not slot_deferred:
                    _close_twitter_pipe(process.stdout)
                    _close_twitter_pipe(process.stderr)
                    _TWITTER_PROCESS_SLOTS.release()

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
    if not passed and not unsupported:
        raise GroundingFailure("failing verdict must identify a claim")
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
        (r"\ban\s+unsupported\s+claim\b", "a statement I could not verify"),
        (r"\bunsupported\s+claim\b", "statement I could not verify"),
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
    """Project exact Gecko pool lists into bounded, valid evidence JSON.

    The transport byte ledger still charges the complete response. Projection
    only removes unrelated pool rows and fields before the evidence-item cap can
    cut JSON mid-object; contract binding is rechecked by the trusted adapter.
    """
    if not isinstance(text, str):
        return text
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
