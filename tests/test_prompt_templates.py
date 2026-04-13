"""Validate all prompt template files load without errors.

Iterates every .md file under prompts/, loads it via load_prompt(),
and verifies that .format() doesn't crash when given dummy values
for all placeholders. Catches brace-escaping mistakes before deploy.
"""
import re
from pathlib import Path

import pytest

from prompt_loader import load_prompt, _cache, _PROMPT_DIR


def _find_all_templates():
    """Find all .md files under prompts/, excluding knowledge/ (different format)."""
    templates = []
    for f in sorted(_PROMPT_DIR.rglob("*.md")):
        rel = f.relative_to(_PROMPT_DIR).with_suffix("")
        # Skip knowledge files — they use a different format (keywords header)
        if "knowledge" in str(rel):
            continue
        templates.append(str(rel))
    return templates


def _extract_placeholders(template_text: str) -> set[str]:
    """Extract placeholder names from a template string.

    Finds {name} patterns that aren't escaped (not {{ or }}).
    """
    # Remove escaped braces first
    cleaned = template_text.replace("{{", "").replace("}}", "")
    return set(re.findall(r"\{(\w+)\}", cleaned))


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear prompt cache before each test."""
    _cache.clear()
    yield
    _cache.clear()


@pytest.mark.parametrize("template_name", _find_all_templates())
def test_template_loads_with_dummy_vars(template_name):
    """Every template loads successfully with dummy placeholder values."""
    raw = (_PROMPT_DIR / f"{template_name}.md").read_text()
    placeholders = _extract_placeholders(raw)
    dummy_kwargs = {name: f"<{name}>" for name in placeholders}
    # This should not raise KeyError or ValueError
    result = load_prompt(template_name, **dummy_kwargs)
    assert isinstance(result, str)
    assert len(result) > 0
    # Verify all dummy values appear in output
    for name in placeholders:
        assert f"<{name}>" in result, f"Placeholder {{{name}}} not filled in {template_name}"


def test_knowledge_files_have_valid_format():
    """Knowledge topic files have valid 'keywords: ...' header and '---' separator."""
    knowledge_dir = _PROMPT_DIR / "bot" / "knowledge"
    if not knowledge_dir.exists():
        pytest.skip("Knowledge directory not created yet")
    for f in sorted(knowledge_dir.glob("*.md")):
        raw = f.read_text()
        lines = raw.split("\n", 2)
        assert len(lines) >= 3, f"{f.name}: needs at least 3 lines (keywords, ---, content)"
        assert lines[0].startswith("keywords:"), f"{f.name}: first line must start with 'keywords:'"
        assert lines[1].strip() == "---", f"{f.name}: second line must be '---'"
        keywords = lines[0].removeprefix("keywords:").strip()
        assert len(keywords) > 0, f"{f.name}: keywords line is empty"
        content = lines[2]
        assert len(content.strip()) > 0, f"{f.name}: content body is empty"
