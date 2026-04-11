"""Tests for AgentDB — UNIQUE constraint on posted_articles.url."""


def test_save_posted_returns_true_on_first_insert(tmp_db):
    assert tmp_db.save_posted("https://example.com/article1", "Test headline") is True


def test_save_posted_returns_false_on_duplicate(tmp_db):
    tmp_db.save_posted("https://example.com/dup", "First headline")
    assert tmp_db.save_posted("https://example.com/dup", "Different headline") is False


def test_was_url_posted_after_save(tmp_db):
    assert tmp_db.was_url_posted("https://example.com/new") is False
    tmp_db.save_posted("https://example.com/new", "Headline")
    assert tmp_db.was_url_posted("https://example.com/new") is True


def test_different_urls_both_succeed(tmp_db):
    assert tmp_db.save_posted("https://example.com/a", "A") is True
    assert tmp_db.save_posted("https://example.com/b", "B") is True


def test_get_recent_runs(tmp_db):
    """get_recent_runs returns completed runs in descending order with expected keys."""
    # Start and finish 3 runs with different stats
    for i in range(3):
        rid = tmp_db.start_run()
        tmp_db.finish_run(rid, collected=i * 10, newsworthy=i, posted=i, voted=0, commented=0)

    runs = tmp_db.get_recent_runs(3)
    assert len(runs) == 3
    # Most recent first
    assert runs[0]["messages_collected"] == 20
    assert runs[1]["messages_collected"] == 10
    assert runs[2]["messages_collected"] == 0
    # All expected keys present
    for r in runs:
        assert "id" in r
        assert "started_at" in r
        assert "finished_at" in r
        assert "articles_posted" in r
