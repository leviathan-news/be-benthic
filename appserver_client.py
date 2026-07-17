"""Minimal JSON-RPC client for ``codex app-server`` over stdio."""

import json
import subprocess
import threading


class AppServerError(Exception):
    """Error raised for transport failures, request timeouts, and RPC errors."""

    def __init__(self, message, rpc_code=None):
        super().__init__(message)
        self.rpc_code = rpc_code


class AppServerClient:
    """Line-delimited JSON-RPC client for a spawned app-server process."""

    _INIT_TIMEOUT = 30.0
    _CLOSE_GRACE = 0.2
    _JOIN_TIMEOUT = 1.0

    def __init__(self, cmd, cwd, env, on_notification=None):
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.on_notification = on_notification

        # The lock protects request id allocation, pending-request bookkeeping,
        # writes to stdin, and the closed flag. The reader thread sets pending
        # results from stdout while caller threads wait on per-request events.
        self._lock = threading.RLock()
        self._next_id = 1
        self._pending = {}
        self._closed = False
        self._proc = None
        self._reader_thread = None
        self._stderr_thread = None
        self._stderr = []

    def start(self):
        """Start the process, begin reader threads, and perform the handshake."""

        with self._lock:
            if self._proc is not None:
                raise AppServerError("codex app-server client already started.")
            self._proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        # stdout carries newline-delimited JSON messages. The daemon reader is
        # the only consumer of stdout and dispatches each complete line.
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()

        # stderr is drained on a separate daemon so a noisy server cannot block
        # on a full stderr pipe. EOF on stderr is treated as connection closure.
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "title": "Benthic Builder",
                        "name": "benthic-builder",
                        "version": "1",
                    },
                    "capabilities": {
                        "experimentalApi": False,
                        "optOutNotificationMethods": [
                            "item/agentMessage/delta",
                            "item/reasoning/summaryTextDelta",
                            "item/reasoning/summaryPartAdded",
                            "item/reasoning/textDelta",
                        ],
                    },
                },
                timeout=self._INIT_TIMEOUT,
            )
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise

    def request(self, method, params, timeout):
        """Send a request and wait for the matching response id."""

        event = threading.Event()
        entry = {"event": event, "result": None, "error": None, "method": method}

        with self._lock:
            self._raise_if_closed()
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = entry
            try:
                self._write_message_locked({"id": request_id, "method": method, "params": params})
            except Exception as exc:
                self._pending.pop(request_id, None)
                error = self._transport_error(f"failed to send {method}: {exc}")
                self._mark_closed(error)
                raise error

        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise AppServerError(f"codex app-server request timed out: {method}")

        if entry["error"] is not None:
            raise entry["error"]
        return entry["result"] if entry["result"] is not None else {}

    def notify(self, method, params=None):
        """Send a notification without registering a response id."""

        with self._lock:
            if self._closed:
                return
            try:
                self._write_message_locked({"method": method, "params": params or {}})
            except Exception as exc:
                self._mark_closed(self._transport_error(f"failed to send {method}: {exc}"))

    def close(self):
        """Close stdin, terminate a still-running process, and join readers."""

        proc = self._proc
        if proc is None:
            return

        self._mark_closed(AppServerError("codex app-server client closed."))

        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass

        try:
            proc.wait(timeout=self._CLOSE_GRACE)
        except subprocess.TimeoutExpired:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=self._CLOSE_GRACE)
            except subprocess.TimeoutExpired:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=self._CLOSE_GRACE)

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=self._JOIN_TIMEOUT)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=self._JOIN_TIMEOUT)

    def _read_stdout(self):
        """Read stdout one line at a time and dispatch JSON-RPC messages."""

        try:
            stream = self._proc.stdout if self._proc is not None else None
            if stream is None:
                self._mark_closed(self._transport_error("codex app-server stdout is unavailable."))
                return
            for line in stream:
                self._handle_line(line)
            self._mark_closed(self._transport_error("codex app-server stdout closed."))
        except Exception as exc:
            self._mark_closed(self._transport_error(f"codex app-server stdout reader failed: {exc}"))

    def _read_stderr(self):
        """Drain stderr and mark the transport closed when that pipe reaches EOF."""

        try:
            stream = self._proc.stderr if self._proc is not None else None
            if stream is None:
                return
            data = stream.read()
            if data:
                self._stderr.append(data)
            self._mark_closed(self._transport_error("codex app-server stderr closed."))
        except Exception as exc:
            self._mark_closed(self._transport_error(f"codex app-server stderr reader failed: {exc}"))

    def _handle_line(self, line):
        """Classify a JSONL message as request, response, or notification."""

        if not line.strip():
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            self._mark_closed(self._transport_error(f"failed to parse codex app-server JSONL: {exc}"))
            return

        has_id = "id" in msg
        has_method = bool(msg.get("method"))

        # A message with both id and method is a server-to-client request. This
        # client has no callable methods, so it mirrors the JS reference client
        # and sends a JSON-RPC Method Not Found error response.
        if has_id and has_method:
            self._reply_unsupported_request(msg)
            return

        # A message with id but no method is a response. Unknown ids are ignored
        # because the caller may already have timed out and removed the entry.
        if has_id:
            self._resolve_response(msg)
            return

        # A method without an id is a notification. Notifications are delivered
        # to the settable callback on the reader thread.
        if has_method and self.on_notification is not None:
            try:
                self.on_notification(msg)
            except Exception as exc:
                self._mark_closed(self._transport_error(f"notification handler failed: {exc}"))

    def _reply_unsupported_request(self, msg):
        with self._lock:
            if self._closed:
                return
            try:
                self._write_message_locked(
                    {
                        "id": msg.get("id"),
                        "error": {
                            "code": -32601,
                            "message": f"Unsupported server request: {msg.get('method')}",
                        },
                    }
                )
            except Exception as exc:
                self._mark_closed(self._transport_error(f"failed to reply to server request: {exc}"))

    def _resolve_response(self, msg):
        with self._lock:
            entry = self._pending.pop(msg.get("id"), None)
        if entry is None:
            return

        if msg.get("error"):
            error = msg["error"]
            entry["error"] = AppServerError(
                error.get("message", f"codex app-server {entry['method']} failed."),
                rpc_code=error.get("code"),
            )
        else:
            entry["result"] = msg.get("result", {})
        entry["event"].set()

    def _write_message_locked(self, msg):
        stdin = self._proc.stdin if self._proc is not None else None
        if stdin is None:
            raise OSError("stdin is unavailable")
        stdin.write(json.dumps(msg) + "\n")
        stdin.flush()

    def _raise_if_closed(self):
        if self._closed:
            raise self._transport_error("codex app-server client is closed.")
        if self._proc is not None and self._proc.poll() is not None:
            self._mark_closed(self._transport_error("codex app-server process exited."))
            raise self._transport_error("codex app-server process exited.")

    def _mark_closed(self, error):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for entry in pending:
            entry["error"] = error
            entry["event"].set()

    def _transport_error(self, message):
        detail = "".join(self._stderr).strip()
        if detail:
            message = f"{message} stderr: {detail}"
        return AppServerError(message)
