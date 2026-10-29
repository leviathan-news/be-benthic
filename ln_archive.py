"""Bound public LN archive pages without treating a sample as the whole feed."""

from datetime import date, datetime, timezone
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_HOST = "api.leviathannews.xyz"
_PATHS = {"/api/v1/news/", "/api/v1/news/search/"}
_KEYS = {"q", "tags", "author", "date_from", "date_to", "sort_type",
         "page", "per_page", "status", "tag_mode"}


def archive_query(url):
    """Return a validated public archive query, or None for another source."""
    parsed = urlsplit(url)
    if parsed.hostname != _HOST or parsed.path not in _PATHS:
        return None
    if (parsed.scheme != "https" or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError("invalid archive URL")
    pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=16)
    query = dict(pairs)
    if len(query) != len(pairs) or set(query) - _KEYS:
        raise ValueError("unsupported archive query")
    if any(not value or len(value) > 160 or any(ord(c) < 32 for c in value)
           for value in query.values()):
        raise ValueError("invalid archive query value")
    if query.get("status", "approved") != "approved":
        raise ValueError("archive evidence requires public approved news")
    if query.get("sort_type", "new") not in {"new", "relevance"}:
        raise ValueError("unsupported archive sample ordering")
    if query.get("tag_mode", "any") not in {"any", "all"}:
        raise ValueError("invalid archive tag mode")
    for key, maximum in (("page", 100_000), ("per_page", 100)):
        if key in query and (not re.fullmatch(r"[1-9][0-9]*", query[key])
                             or int(query[key]) > maximum):
            raise ValueError("invalid archive page limit")
    for key in ("date_from", "date_to"):
        if key in query and date.fromisoformat(query[key]).isoformat() != query[key]:
            raise ValueError("invalid archive date")
    if query.get("date_from", "0001-01-01") > query.get("date_to", "9999-12-31"):
        raise ValueError("reversed archive dates")
    if parsed.path.endswith("/search/"):
        if not any(query.get(key) for key in ("q", "tags", "author")):
            raise ValueError("archive search requires a topic or author filter")
    elif "date_from" in query or "date_to" in query:
        # The feed silently ignores date filters. Only search implements them.
        raise ValueError("date filters require archive search")
    return query


def archive_url(url):
    query = archive_query(url)
    if query is None:
        return None
    parsed = urlsplit(url)
    return urlunsplit(("https", _HOST, parsed.path, urlencode(sorted(query.items())), ""))


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing archive timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("archive timestamp has no timezone")
    return stamp.astimezone(timezone.utc).isoformat()


def project_archive(url, text):
    """Keep dated headlines and attributed excerpts inside the 8 KiB allowance.

    All transport bytes are still charged by SafeHttpFetcher. Counts describe
    this filtered API result, never the entire archive or a representative audit.
    """
    query = archive_query(url)
    if query is None:
        return None
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate archive JSON key")
        return result
    def invalid_constant(value):
        raise ValueError("invalid archive JSON number")
    data = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("invalid archive result")
    count, page = data.get("count"), data.get("current_page")
    if (type(count) is not int or count < len(rows)
            or type(page) is not int or page != int(query.get("page", "1"))):
        raise ValueError("archive pagination does not match request")
    if "q" in query and data.get("query") != query["q"]:
        raise ValueError("archive search does not match request")
    if "sort_type" in query and data.get("sort_type") != query["sort_type"]:
        raise ValueError("archive ordering does not match request")
    scope = {
        "source_url": archive_url(url), "query": query,
        "matching_count": count, "page": page, "returned_rows": len(rows),
        "omitted_rows": 0,
        "coverage": "A filtered, ordered page sample, not a random or exhaustive feed audit. "
                    "Headlines establish what LN published, not the truth of the underlying claims. "
                    "Displayed comments are excerpts; missing excerpts do not prove no comments exist.",
    }
    result = {"archive_scope": scope, "results": []}
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid archive article")
        article_id = row.get("id")
        if (type(article_id) is not int or article_id <= 0 or article_id in seen
                or row.get("status") != "approved" or row.get("content_type") != "news"):
            raise ValueError("invalid or repeated public archive article")
        seen.add(article_id)
        stamp = _timestamp(row.get("date_posted") or row.get("date_created"))
        if not query.get("date_from", "0001-01-01") <= stamp[:10] <= query.get("date_to", "9999-12-31"):
            raise ValueError("archive article lies outside the requested dates")
        headline = _text(row.get("headline"), 500)
        if not headline.strip():
            raise ValueError("missing archive headline")
        user = row.get("post_user") or {}
        clean = {
            "id": article_id, "article_url": f"https://leviathannews.xyz/{article_id}",
            "headline": headline, "headline_truncated": headline != row["headline"],
            "date_posted": stamp, "source": _text(row.get("source"), 80),
            "author": _text(user.get("display_name"), 80) if isinstance(user, dict) else "",
            "tags": [_text(tag.get("name"), 60) for tag in (row.get("tags") or [])[:8]
                     if isinstance(tag, dict)],
            "top_yaps": [],
        }
        if type(row.get("yap_count")) is int and row["yap_count"] >= 0:
            clean["yap_count"] = row["yap_count"]
        for comment in (row.get("top_yaps") or [])[:2]:
            if not isinstance(comment, dict) or not isinstance(comment.get("author"), dict):
                raise ValueError("invalid archive comment attribution")
            body = _text(comment.get("text"), 320)
            if not body:
                continue
            clean["top_yaps"].append({
                "author": _text(comment["author"].get("display_name"), 80),
                "created_at": _timestamp(comment.get("created_at")),
                "text": body, "truncated": body != comment["text"],
                "url": f"https://{_HOST}/api/v1/news/{article_id}/list_yaps",
            })
        result["results"].append(clean)
    def render():
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    while len(render().encode("utf-8")) > 7_800 and result["results"]:
        result["results"].pop()
        scope["omitted_rows"] += 1
    if len(render().encode("utf-8")) > 7_800:
        raise ValueError("archive scope exceeds evidence allowance")
    return render()
