"""Tests for AgentDB — posted_articles dedup and self-dedup."""


def test_save_posted_inserts(tmp_db):
    tmp_db.save_posted("https://example.com/article1", "Test headline")
    assert tmp_db.was_url_posted("https://example.com/article1") is True


def test_was_url_posted_prevents_duplicates(tmp_db):
    """Dedup is handled by was_url_posted() before save_posted()."""
    tmp_db.save_posted("https://example.com/dup", "First headline")
    # was_url_posted detects the duplicate
    assert tmp_db.was_url_posted("https://example.com/dup") is True


def test_was_url_posted_after_save(tmp_db):
    assert tmp_db.was_url_posted("https://example.com/new") is False
    tmp_db.save_posted("https://example.com/new", "Headline")
    assert tmp_db.was_url_posted("https://example.com/new") is True


def test_different_urls_both_succeed(tmp_db):
    tmp_db.save_posted("https://example.com/a", "A")
    tmp_db.save_posted("https://example.com/b", "B")
    assert tmp_db.was_url_posted("https://example.com/a") is True
    assert tmp_db.was_url_posted("https://example.com/b") is True


def test_was_story_posted(tmp_db):
    """Self-dedup: rejects same story from different sources."""
    tmp_db.save_posted("https://example.com/original", "Bitcoin ETF approval delayed by SEC again",
                       story_hint="Bitcoin ETF SEC approval delay")
    # Same story, different source — should match
    assert tmp_db.was_story_posted("Bitcoin ETF SEC delays approval decision") is True
    # Completely different story — should not match
    assert tmp_db.was_story_posted("Ethereum Pectra upgrade scheduled for May") is False
    # Single common word — should not match (requires >=2)
    assert tmp_db.was_story_posted("Bitcoin price hits new high") is False
