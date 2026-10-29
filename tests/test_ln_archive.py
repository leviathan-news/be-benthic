"""Public LN archive evidence retains dated samples without bulky UI metadata."""

import json

import pytest

from reply_grounding import GroundingFailure, compact_machine_source_text


URL = ("https://api.leviathannews.xyz/api/v1/news/search/"
       "?tags=bitcoin,ethereum,defi&date_from=2026-03-26&date_to=2026-09-26"
       "&sort_type=new&per_page=5")


def article(article_id=305156, **values):
    row = {
        "id": article_id, "content_type": "news", "status": "approved",
        "headline": "NetNet opens tokenized-equity lending markets",
        "date_posted": "2026-09-14T05:00:00+00:00",
        "post_user": {"id": 1425, "display_name": "Contributor"},
        "source": "NetNet", "tags": [{"name": "DeFi", "slug": "defi"}],
        "yap_count": 2,
        "top_yaps": [{"id": 305252, "text": "Equity feeds pause outside market hours.",
                      "created_at": "2026-09-14T05:51:56+00:00",
                      "author": {"id": 1802, "display_name": "Benthic"}}],
        "media_meta": {"large": "x" * 20_000},
        "vote_details": {"large": "x" * 20_000},
    }
    row.update(values)
    return row


def payload(rows=None, **values):
    data = {"count": 700, "total_pages": 140, "current_page": 1,
            "next": 2, "previous": None, "sort_type": "new", "query": "",
            "results": rows if rows is not None else [article()]}
    data.update(values)
    return data


def project(data=None, url=URL):
    return json.loads(compact_machine_source_text(url, json.dumps(data or payload())))


def test_archive_projection_retains_dates_attribution_links_and_sampling_scope():
    result = project()
    scope = result["archive_scope"]
    assert scope["query"]["date_from"] == "2026-03-26"
    assert scope["query"]["date_to"] == "2026-09-26"
    assert scope["query"]["tags"] == "bitcoin,ethereum,defi"
    assert scope["matching_count"] == 700
    assert scope["returned_rows"] == 1
    assert "sample" in scope["coverage"]
    row = result["results"][0]
    assert row["date_posted"] == "2026-09-14T05:00:00+00:00"
    assert row["article_url"] == "https://leviathannews.xyz/305156"
    assert row["top_yaps"][0]["author"] == "Benthic"
    assert row["top_yaps"][0]["created_at"] == "2026-09-14T05:51:56+00:00"
    assert row["top_yaps"][0]["url"].endswith("/305156/list_yaps")
    assert "media_meta" not in row and "vote_details" not in row


def test_large_unicode_archive_remains_complete_json_with_explicit_omissions():
    rows = [article(n, headline="海" * 3000, top_yaps=[]) for n in range(1, 21)]
    text = compact_machine_source_text(URL.replace("per_page=5", "per_page=20"),
                                      json.dumps(payload(rows)))
    assert len(text.encode("utf-8")) < 8_000
    result = json.loads(text)
    scope = result["archive_scope"]
    assert scope["returned_rows"] == 20
    assert scope["omitted_rows"] == 20 - len(result["results"])
    assert scope["omitted_rows"] > 0
    assert all(row["headline_truncated"] for row in result["results"])


@pytest.mark.parametrize("change", [
    {"date_posted": "2025-06-01T00:00:00+00:00"},
    {"date_posted": "bad-date"}, {"status": "preview"},
    {"content_type": "yap"}, {"id": True},
])
def test_archive_rejects_wrong_period_or_unpublished_or_malformed_rows(change):
    with pytest.raises(GroundingFailure):
        project(payload([article(**change)]))


@pytest.mark.parametrize("change", [
    {"current_page": 2}, {"count": 0}, {"results": [article(), article()]},
])
def test_archive_rejects_repeated_rows_and_pagination_drift(change):
    with pytest.raises(GroundingFailure):
        project(payload(**change))


def test_empty_archive_remains_an_empty_scoped_result():
    result = project(payload([], count=0, total_pages=1, next=None))
    assert result["results"] == []
    assert result["archive_scope"]["matching_count"] == 0


def test_other_hosts_do_not_receive_ln_archive_authority():
    raw = json.dumps(payload())
    assert compact_machine_source_text(URL.replace("api.leviathannews.xyz",
                                                   "api.example.org"), raw) == raw


@pytest.mark.parametrize("suffix", ["&token=secret", "&date_to=2025-01-01", "&status=all"])
def test_archive_rejects_unsupported_or_duplicate_query_fields(suffix):
    with pytest.raises(GroundingFailure):
        project(url=URL + suffix)
