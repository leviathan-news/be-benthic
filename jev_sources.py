"""Bounded source reads for ambiguous news triage, using the grounded readers."""

from collections import OrderedDict
from dataclasses import replace
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from reply_grounding import (
    GroundingFailure, GroundingLimits, SafeHttpFetcher, canonical_source_url,
    parse_x_status_url, resolve_twitter_fetcher, truncate_utf8,
)


def source_text(value, limit=6000):
    """Keep source data inside its JSON field and the existing prompt boundaries."""
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or ""))
    value = value.encode("utf-8", errors="ignore").decode("utf-8")
    value = truncate_utf8(value, limit).replace("<", "＜").replace(">", "＞")
    return re.sub(r"={4,}", "===", re.sub(r"-{4,}", "---", value)).strip()


class NewsSourceReader:
    def __init__(self):
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def fetch(self, url, *, deadline, max_response_bytes):
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (any(host == root or host.endswith("." + root) for root in ("x.com", "twitter.com"))
                and re.fullmatch(r"/i/web/status/[0-9]+/?", parsed.path)):
            url = urlunsplit(parsed._replace(path=parsed.path.replace("/i/web/", "/i/", 1)))
        canonical = canonical_source_url(url)
        with self._lock:
            cached = self._cache.get(canonical)
            if cached and time.monotonic() - cached[0] < 300:
                self._cache.move_to_end(canonical)
                return replace(cached[1], response_bytes=0)
        if deadline <= time.monotonic() or max_response_bytes <= 0:
            raise GroundingFailure("source recovery budget exhausted")
        if parse_x_status_url(canonical):
            reader = resolve_twitter_fetcher(timeout=10)
        else:
            reader = SafeHttpFetcher(limits=GroundingLimits(
                fetch_timeout=10, max_response_bytes=524288,
                max_source_bytes=1048576, max_redirects=2))
        # Readers are per-call: their mutable deadlines and byte counters must
        # never be shared between the two recovery workers.
        reader.collection_deadline = deadline
        fetched = reader.fetch(canonical, max_response_bytes=max_response_bytes)
        with self._lock:
            self._cache[canonical] = (time.monotonic(), fetched)
            self._cache.move_to_end(canonical)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
        return fetched
