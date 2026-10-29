Your previous response was not valid JSON. The parser failed.
Return ONLY a JSON array — no prose, no notes, no markdown.
If nothing is newsworthy, return exactly: []

JSON schema per item: {{"msg_id": <int>, "channel": "@name", "url": "https://url-from-the-message.com/article", "headline_hint": "main entity + action", "heat": <int 1-10; 8+ only market-moving front-page news; genuine news with market relevance (exploits, outages, depegs, funding, listings) is 5-7; 1-4 only for marginal minor updates>, "reason": "why newsworthy"}}

URL RULES: the url MUST appear in the message text, copied exactly — never invent one. Never use t.me/ or leviathannews.xyz URLs or bare social profiles. A message with no usable URL is excluded.

Evaluate these messages:
{formatted}
