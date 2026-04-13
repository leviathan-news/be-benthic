Your previous response was not valid JSON. The parser failed.
Return ONLY a JSON array — no prose, no notes, no markdown.
If nothing is newsworthy, return exactly: []

JSON schema per item: {{"msg_id": <int>, "channel": "@name", "url": "https://primary-source.com/article", "headline_hint": "main entity + action", "reason": "why newsworthy"}}

URL RULES: Never use t.me/ or leviathannews.xyz URLs. Find the original article/tweet URL.

Evaluate these messages:
{formatted}