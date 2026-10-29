# Public page reader

`SafeHttpFetcher` first extracts visible static content. When an HTML page has
only an application shell or an unrendered article, it can use
`BrowserSourceReader` to obtain a bounded visible-text snapshot. Navigation
does not count as article content. Password forms and common access gates are
unavailable sources; the reader does not authenticate or bypass challenges.

The browser runs as UID 1000 in a disposable, read-only container with no
network, host mounts, credentials, Linux capabilities, or Docker socket.
Chromium's own sandbox remains enabled. The host broker accepts only bounded
GET requests for documents, scripts, stylesheets, fetches and XHRs. Every
resource uses the same public-address validation, IP pinning, TLS verification,
redirect limits, absolute deadline and aggregate byte ledger as static reads.
Browser resources also consume the turn's existing source-request allowance.
Cookies, browser headers and page credentials never reach the broker.

One process slot permits one render at a time. Each container has 768 MiB of
memory, one CPU, 128 processes and a bounded temporary filesystem. The protocol
caps output at 65,536 bytes, requests at 32, and article text at 8,000 UTF-8
bytes. The normal turn limit of ten source requests remains stricter. Capacity
returns only after cleanup is confirmed. Failed resources, redirected assets, clipping, or content
that does not settle within the bounded observation window mark the result as
an excerpt. A settled snapshot is not proof that a dynamic page can never add
more content.

The broker follows asset redirects itself and serves their final bytes at the
requested browser URL. Relative imports in redirected stylesheets or modules
may therefore be incomplete; these captures always carry the excerpt flag.

The base image is pinned by digest and Python dependencies by version and
wheel hashes. `seccomp.json` derives from Playwright v1.63.0's Docker profile;
the additional `chroot` permission supports Chromium's sandbox inside its new
user namespace, without adding container capabilities.

Build on the runtime Linux host:

```sh
docker build -t benthic-source-reader:20260922 browser-reader
```

The runtime needs `browser_reader.py` and `browser-reader/seccomp.json` beside
`reply_grounding.py`. `GROUNDING_BROWSER_READER=1` enables the adapter by
default. Setting it to `0` restores static-only reading without disabling
grounding or verification.

Run the actual image tests from a Python virtual environment with project test
dependencies:

```sh
BENTHIC_REPLAY_BROWSER=1 .venv/bin/python -m pytest -q tests/test_browser_reader_container.py tests/test_reply_replays.py
```

These tests check streamed content, hidden text, access gates, delayed content,
UTF-8 clipping, resource redirects, deadline cleanup, and ingress-to-answer
replay. Replays isolate SQLite state and replace publication with an in-memory
capture. `scripts/eval_reply_replays.py --live-providers` additionally evaluates
real tools-disabled composer/verifier output with an independent task-completion
judge; it sends no Telegram messages.
