"""Tests for the prompt template loader."""
import os
import tempfile
from pathlib import Path

import pytest


def test_load_prompt_plain():
    """Load a template with no placeholders."""
    from prompt_loader import load_prompt, _cache, _PROMPT_DIR
    _cache.clear()
    test_dir = _PROMPT_DIR / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = test_dir / "plain.md"
    test_file.write_text("Hello world, no placeholders here.")
    try:
        result = load_prompt("test/plain")
        assert result == "Hello world, no placeholders here."
    finally:
        test_file.unlink()
        test_dir.rmdir()
        _cache.clear()


def test_load_prompt_with_vars():
    """Load a template and fill placeholders."""
    from prompt_loader import load_prompt, _cache, _PROMPT_DIR
    _cache.clear()
    test_dir = _PROMPT_DIR / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = test_dir / "vars.md"
    test_file.write_text("Hello {name}, today is {date}.")
    try:
        result = load_prompt("test/vars", name="TestAgent", date="2026-04-12")
        assert result == "Hello TestAgent, today is 2026-04-12."
    finally:
        test_file.unlink()
        test_dir.rmdir()
        _cache.clear()


def test_load_prompt_escaped_braces():
    """Literal braces in template (JSON examples) survive .format()."""
    from prompt_loader import load_prompt, _cache, _PROMPT_DIR
    _cache.clear()
    test_dir = _PROMPT_DIR / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = test_dir / "braces.md"
    test_file.write_text('Return JSON: {{"score": 7, "name": "{name}"}}')
    try:
        result = load_prompt("test/braces", name="test")
        assert result == 'Return JSON: {"score": 7, "name": "test"}'
    finally:
        test_file.unlink()
        test_dir.rmdir()
        _cache.clear()


def test_load_prompt_caches():
    """Second call returns cached result without re-reading file."""
    from prompt_loader import load_prompt, _cache, _PROMPT_DIR
    _cache.clear()
    test_dir = _PROMPT_DIR / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = test_dir / "cached.md"
    test_file.write_text("original")
    try:
        result1 = load_prompt("test/cached")
        test_file.write_text("modified")
        result2 = load_prompt("test/cached")
        assert result1 == result2 == "original"
    finally:
        test_file.unlink()
        test_dir.rmdir()
        _cache.clear()


def test_load_prompt_missing_file():
    """Missing template raises FileNotFoundError — no silent fallback."""
    from prompt_loader import load_prompt, _cache
    _cache.clear()
    with pytest.raises(FileNotFoundError):
        load_prompt("nonexistent/template")


def test_load_prompt_missing_var():
    """Missing placeholder variable raises KeyError."""
    from prompt_loader import load_prompt, _cache, _PROMPT_DIR
    _cache.clear()
    test_dir = _PROMPT_DIR / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = test_dir / "missing_var.md"
    test_file.write_text("Hello {name}, you are {role}.")
    try:
        with pytest.raises(KeyError):
            load_prompt("test/missing_var", name="TestAgent")
    finally:
        test_file.unlink()
        test_dir.rmdir()
        _cache.clear()
