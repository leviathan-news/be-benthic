"""Trusted container worker. Page JavaScript has no network or host authority."""

import base64
import json
from pathlib import Path
import sys
import time

from playwright.sync_api import sync_playwright

EXTRACT = (Path(__file__).parent / "extract.js").read_text()


def send(value):
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")), flush=True)


def receive(limit):
    line = sys.stdin.buffer.readline(limit + 1)
    if not line.endswith(b"\n") or len(line) > limit:
        raise ValueError("protocol size")
    return json.loads(line)


def main():
    config = receive(4096)
    url = config["url"]
    timeout = min(30_000, max(1, config["timeout_ms"]))
    with sync_playwright() as pw:
        # The outer container supplies isolation without host networking,
        # mounts, privileges or credentials. Chromium keeps its own sandbox
        # where the host supports unprivileged namespaces.
        browser = pw.chromium.launch(headless=True, chromium_sandbox=True)
        context = browser.new_context(service_workers="block", accept_downloads=False,
                                      permissions=[], color_scheme="light")
        context.set_default_timeout(timeout)
        page = context.new_page()
        requests = 0
        resource_limit_hit = False
        pending = set()
        page.on("request", lambda request: pending.add(request))
        page.on("requestfinished", lambda request: pending.discard(request))
        page.on("requestfailed", lambda request: pending.discard(request))

        def route_request(route):
            nonlocal requests, resource_limit_hit
            req = route.request
            if (req.method != "GET" or req.resource_type not in
                    {"document", "script", "stylesheet", "fetch", "xhr"}
                    or not req.url.startswith(("https://", "http://"))
                    or (req.is_navigation_request() and
                        (req.frame != page.main_frame or
                         (req.url != url and req.redirected_from is None)))):
                route.abort()
                return
            if requests >= 32:
                resource_limit_hit = True
                route.abort()
                return
            requests += 1
            send({"type": "fetch", "url": req.url, "method": req.method,
                  "resource_type": req.resource_type})
            response = receive(2_200_000)
            if response.get("blocked"):
                route.abort()
            else:
                route.fulfill(status=200, body=base64.b64decode(response["body"], validate=True),
                              content_type=response["content_type"])

        context.route("**/*", route_request)
        context.route_web_socket("**/*", lambda ws: ws.close())
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        # A rendered introduction is not proof that article hydration finished.
        # Observe a bounded quiet window, including brokered resource activity;
        # a usable but unsettled capture remains explicitly excerpt-only.
        started = time.monotonic()
        settle_deadline = started + min(2.5, timeout / 1000)
        previous, unchanged_since, settled = None, started, False
        result = {"usable": False}
        while time.monotonic() < settle_deadline:
            result = page.evaluate(EXTRACT)
            now = time.monotonic()
            signature = (result.get("usable"), result.get("text"))
            if signature != previous or pending:
                unchanged_since = now
                previous = signature
            if result.get("usable") and not pending and now - started >= 1 and now - unchanged_since >= .75:
                settled = True
                break
            page.wait_for_timeout(100)
        if not result.pop("usable"):
            raise ValueError("content not ready")
        result["truncated"] = result["truncated"] or resource_limit_hit or not settled
        browser.close()
        send(result)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Raw exceptions can contain source content, addresses and paths.
        send({"type": "error", "reason": "render_failed"})
        raise SystemExit(1)
