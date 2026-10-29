"""A failed source cannot erase evidence collected for independent task parts."""

from dataclasses import replace

import pytest

import reply_grounding as rg


def message(text):
    return {"message_id": 1, "chat": {"id": -123, "is_forum": False},
            "from": {"id": 42}, "text": text}


@pytest.mark.parametrize("failed_first", [True, False])
def test_partial_focal_failure_preserves_readable_source_and_failure(failed_first):
    good, bad = "https://example.com/readable", "https://example.com/unavailable"
    attempted = []

    def fetch(url, focal):
        attempted.append(url)
        if url == bad:
            raise rg.GroundingFailure("untrusted exception with secret text")
        return rg.FetchedSource(url, "web:" + "a" * 20, "The useful article.")

    urls = [bad, good] if failed_first else [good, bad]
    evidence = rg.collect_evidence(message("Discuss " + " and ".join(urls)), [], [],
                                   direct=True, mode="grounded", url_fetcher=fetch)
    assert attempted == urls
    assert [x.text for x in evidence.items if x.kind == "focal_url"] == ["The useful article."]
    assert evidence.focal_source_urls == tuple(urls)
    assert len(evidence.source_failures) == 1
    failure = evidence.source_failures[0]
    assert failure.canonical_url == bad
    assert failure.failure_kind == "source_unavailable"
    receipt = next(x for x in evidence.items if x.source_ref.startswith("runtime:source_failure:"))
    assert bad in receipt.text
    assert "secret" not in receipt.text


def test_all_focal_failures_still_block_unrelated_background():
    attempted = []

    def fetch(url, focal):
        attempted.append(url)
        raise rg.GroundingFailure("response contains no visible text")

    with pytest.raises(rg.GroundingFailure, match="no visible text"):
        rg.collect_evidence(message("https://example.com/a https://example.com/b"), [], [],
                            direct=True, mode="grounded", url_fetcher=fetch,
                            background_urls=["https://example.com/background"])
    assert attempted == ["https://example.com/a", "https://example.com/b"]


def test_partial_source_failure_receipts_cannot_be_evicted_as_background():
    def fetch(url, focal):
        if url.endswith("bad"):
            raise rg.GroundingFailure("unavailable")
        return rg.FetchedSource(url, "web:" + "b" * 20, "short source")

    source = rg.collect_evidence(message("https://example.com/good https://example.com/bad"), [], [],
                                 direct=True, mode="grounded", url_fetcher=fetch)
    receipt = next(x for x in source.items if x.source_ref.startswith("runtime:source_failure:"))
    with pytest.raises(rg.GroundingFailure, match="essential evidence"):
        rg._fit_evidence(source.items, len(receipt.text.encode()) - 1)

