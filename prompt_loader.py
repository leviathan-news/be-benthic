"""Prompt template loader — reads .md files from prompts/ and fills placeholders.

Templates use Python str.format() syntax: {variable_name} for interpolation,
{{ and }} for literal braces (e.g. JSON examples).

Loaded templates are cached in-memory for the process lifetime (one read per
template, zero I/O overhead on subsequent calls). If a template file is missing,
FileNotFoundError is raised immediately — a missing prompt is a broken deploy.
"""
from pathlib import Path

# Directory containing all prompt template files, resolved relative to this
# module so it works regardless of the working directory at runtime.
_PROMPT_DIR = Path(__file__).parent / "prompts"

# In-memory cache: template name → raw file content (before .format()).
# Never cleared at runtime — templates are static for the process lifetime.
_cache: dict[str, str] = {}


def load_prompt(template_name: str, **kwargs) -> str:
    """Load a prompt template and fill placeholders.

    Resolves the template path as: <_PROMPT_DIR>/<template_name>.md
    Reads the file once and caches the raw content. On subsequent calls the
    cached raw content is used directly, so the file is never re-read.

    Placeholder filling uses str.format(**kwargs): every {variable_name} token
    in the template is replaced with the corresponding kwarg value. Literal
    braces in template content (e.g. JSON examples) must be escaped as {{ and
    }} so that str.format() passes them through as { and }.

    Args:
        template_name: Path relative to prompts/ without .md extension.
                       Example: 'agent/craft_comment', 'bot/knowledge/tipping'
        **kwargs: Template variables to fill via str.format(). Callers may
                  freely use any kwarg name including 'name' — there is no
                  collision with the positional parameter.

    Returns:
        The prompt string with all placeholders filled.

    Raises:
        FileNotFoundError: Template file does not exist (broken deploy).
        KeyError: A placeholder in the template has no matching kwarg.
    """
    # Cache miss: read from disk and store raw content. Any subsequent call
    # with the same template_name uses the cached raw text, bypassing I/O.
    if template_name not in _cache:
        # read_text() raises FileNotFoundError if the path doesn't exist —
        # we intentionally let it propagate: a missing template is a hard error.
        _cache[template_name] = (_PROMPT_DIR / f"{template_name}.md").read_text()

    template = _cache[template_name]

    # Only call .format() when there are variables to substitute. This avoids
    # an unnecessary str scan on plain templates and preserves the fast path
    # for callers that only need the raw text (e.g. soul prompt on every call).
    # str.format() raises KeyError for any {placeholder} with no matching kwarg,
    # which is the desired fail-fast behaviour — missing variables = broken call.
    return template.format(**kwargs) if kwargs else template
