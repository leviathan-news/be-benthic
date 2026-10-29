"""Render public pages in a disposable, network-isolated browser container.

The container receives only the current public document. Its resource requests
are untrusted RPC data; the parent performs credential-free, DNS-pinned GETs.
No browser socket can reach the network, even if request interception fails.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import threading
import time
import uuid

from reply_grounding import GroundingFailure, canonical_source_url


_SLOTS = threading.BoundedSemaphore(1)
_MAX_RESOURCES = 32
_MAX_STDOUT = 65_536
_MAX_TEXT_BYTES = 8_000
_IMAGE = "benthic-source-reader:20260922"
_MIMES = {
    "document": frozenset({"text/html", "application/xhtml+xml"}),
    "script": frozenset({"text/javascript", "application/javascript", "application/x-javascript", "text/ecmascript", "application/ecmascript"}),
    "stylesheet": frozenset({"text/css"}),
    "fetch": frozenset({"application/json", "text/plain", "text/html", "text/x-component"}),
    "xhr": frozenset({"application/json", "text/plain", "text/html", "text/x-component"}),
}


@dataclass(frozen=True)
class RenderedPage:
    text: str
    response_bytes: int
    truncated: bool


def container_command(name):
    """Only fixed runtime flags cross the Docker authority boundary."""
    return [shutil.which("docker") or "/usr/bin/docker", "run", "--rm", "--init", "-i",
            "--name", name, "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=1000:1000",
            "--security-opt=seccomp=" + str(Path(__file__).parent / "browser-reader" / "seccomp.json"),
            "--memory=768m", "--cpus=1", "--pids-limit=128", "--shm-size=128m",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=128m", _IMAGE]


def validate_resource_request(value):
    if (not isinstance(value, dict)
            or set(value) != {"type", "url", "method", "resource_type"}
            or value["type"] != "fetch" or value["method"] != "GET"
            or not isinstance(value["resource_type"], str)
            or value["resource_type"] not in _MIMES):
        raise GroundingFailure("invalid browser resource request")
    return canonical_source_url(value["url"]), _MIMES[value["resource_type"]]


class _Pipe:
    """Bound every byte and wait on the renderer protocol, including writes."""

    def __init__(self, process, deadline):
        self.process, self.deadline = process, deadline
        self.buffer = bytearray()
        self.read_bytes = 0
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stdin.fileno(), False)

    def wait(self, stream, event):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise GroundingFailure("browser reader timed out")
        with selectors.DefaultSelector() as selector:
            selector.register(stream, event)
            if not selector.select(remaining):
                raise GroundingFailure("browser reader timed out")

    def send(self, value):
        payload = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        if len(payload) > 2_200_000:
            raise GroundingFailure("browser resource protocol too large")
        view = memoryview(payload)
        while view:
            self.wait(self.process.stdin, selectors.EVENT_WRITE)
            try:
                sent = os.write(self.process.stdin.fileno(), view)
            except BlockingIOError:
                continue
            if not sent:
                raise GroundingFailure("browser reader closed input")
            view = view[sent:]

    def receive(self):
        while b"\n" not in self.buffer:
            self.wait(self.process.stdout, selectors.EVENT_READ)
            try:
                chunk = os.read(self.process.stdout.fileno(), 4096)
            except BlockingIOError:
                continue
            if not chunk:
                raise GroundingFailure("browser reader closed output")
            self.read_bytes += len(chunk)
            if self.read_bytes > _MAX_STDOUT:
                raise GroundingFailure("browser reader output too large")
            self.buffer.extend(chunk)
        line, _, remaining = self.buffer.partition(b"\n")
        self.buffer = bytearray(remaining)
        try:
            return json.loads(line)
        except (UnicodeError, ValueError) as exc:
            raise GroundingFailure("invalid browser reader output") from exc


def _reap_container(process, name):
    """Return capacity only after both the Docker client and container are gone."""
    env = {"PATH": os.defpath}
    if process.poll() is None:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() == 0:
        # A normally completed attached `docker run --rm` has reaped its
        # container. We never kill the client before removing the container.
        return True
    command = container_command(name)[0]
    try:
        removed = subprocess.run([command, "rm", "-f", name], capture_output=True,
                                 timeout=5, env=env, check=False)
        if removed.returncode:
            state = subprocess.run([command, "ps", "-aq", "--filter", "name=^/" + name + "$"],
                                   capture_output=True, timeout=5, env=env, check=False)
            if state.returncode or state.stdout.strip():
                return False
        process.wait(timeout=2)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


class BrowserSourceReader:
    def render(self, resource, *, fetch_resource, deadline, max_response_bytes):
        if not _SLOTS.acquire(blocking=False):
            raise GroundingFailure("browser reader capacity unavailable")
        process = None
        name = "benthic-reader-" + uuid.uuid4().hex
        reaped = True
        try:
            if deadline <= time.monotonic():
                raise GroundingFailure("browser reader timed out")
            process = subprocess.Popen(container_command(name), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       env={"PATH": os.defpath}, bufsize=0)
            pipe = _Pipe(process, deadline)
            pipe.send({"url": resource.canonical_url,
                       "timeout_ms": max(1, int((deadline - time.monotonic()) * 1000))})
            cache = {resource.canonical_url: resource}
            response_bytes = len(resource.body)
            requests = 0
            partial = False
            while True:
                value = pipe.receive()
                if isinstance(value, dict) and value.get("type") == "result":
                    if (set(value) != {"type", "text", "url", "truncated"}
                            or value["url"] != resource.canonical_url
                            or not isinstance(value["text"], str)
                            or not value["text"].strip()
                            or len(value["text"].encode()) > _MAX_TEXT_BYTES
                            or type(value["truncated"]) is not bool):
                        raise GroundingFailure("invalid rendered source")
                    return RenderedPage(value["text"], response_bytes,
                                        value["truncated"] or partial)
                if isinstance(value, dict) and value.get("type") == "error":
                    raise GroundingFailure("browser could not render source")
                url, mimes = validate_resource_request(value)
                requests += 1
                if requests > _MAX_RESOURCES:
                    raise GroundingFailure("browser resource request limit exceeded")
                try:
                    if url not in cache:
                        fetched = fetch_resource(url, accepted_content_types=mimes)
                        if len(fetched.body) + response_bytes > max_response_bytes:
                            raise GroundingFailure("source byte budget exceeded")
                        response_bytes += len(fetched.body)
                        cache[url] = fetched
                        cache[fetched.canonical_url] = fetched
                    fetched = cache[url]
                    if fetched.content_type.split(";", 1)[0].strip().lower() not in mimes:
                        raise GroundingFailure("unsafe browser resource content type")
                except GroundingFailure:
                    partial = True
                    pipe.send({"blocked": True})
                    continue
                # The trusted transport has already validated every redirect
                # and obtained these bytes. Playwright does not route a second
                # request after a fulfilled redirect, so emitting a 302 would
                # attempt an unbrokered socket in the network-none container.
                # Relative imports may resolve differently under the requested
                # asset URL, so such a capture cannot claim completeness.
                if fetched.canonical_url != url:
                    partial = True
                pipe.send({"body": base64.b64encode(fetched.body).decode(),
                           "content_type": fetched.content_type})
        except GroundingFailure:
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise GroundingFailure("browser reader unavailable") from exc
        finally:
            if process is not None:
                reaped = _reap_container(process, name)
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
            if reaped:
                _SLOTS.release()
            else:
                # Unknown container lifetime holds its slot. A retry must not
                # create an unbounded collection of orphaned browser workers.
                logging.getLogger(__name__).error("Browser cleanup unconfirmed; capacity retained")
