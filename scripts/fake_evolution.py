"""
Fake Evolution API — stands in for the real WhatsApp gateway locally.

The bot posts every outbound message to
    {evolution_api_url}/message/sendText/{instance}
so this serves that one route, logs what it received to stdout, and appends
each message to evolution-inbox.log next to this file so you can read what the
bot would have sent without watching the console.

It does NOT receive inbound WhatsApp messages — Evolution does that. To drive
the bot's conversation locally, POST the webhook payload yourself (see
DEPLOY.md, "Testing the WhatsApp flow").

Run it:  py -3.14 scripts/fake_evolution.py
Then point a tenant at it:  evolution_api_url = http://localhost:9999
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "evolution-inbox.log")


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 with a Content-Length on every response, so the bot's HTTP
    # client can keep the connection open and reuse it. The bot sends two
    # messages a second while the outbox is busy, and a fresh TCP handshake
    # for each one is a lot of noise for something meant to be quiet.
    protocol_version = "HTTP/1.1"

    # A connection that goes quiet mid-request must not hold a thread
    # forever. This is the difference between a gateway that recovers and
    # one that wedges until it is restarted.
    timeout = 10

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
        except (ValueError, OSError):
            # The client vanished or sent a malformed length. Answer nothing
            # and let the connection close — the bot retries on its own.
            self.close_connection = True
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"_raw": raw.decode("utf-8", "replace")}

        number = payload.get("number", "?")
        text = payload.get("text", "")
        print(f"[fake-evolution] -> {number}\n{text}\n{'-' * 60}", flush=True)

        # Reopened per message on purpose: the file is deleted and recreated
        # between test runs, and a long-lived handle would keep writing into
        # an unlinked file.
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(f"=== {number} ===\n{text}\n\n")
        except OSError as exc:
            print(f"[fake-evolution] could not write {LOG_FILE}: {exc}", flush=True)

        body = json.dumps({"key": {"id": "fake-message-id"}}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.close_connection = True

    def log_message(self, format, *args):
        pass  # keep the console readable


class Server(ThreadingHTTPServer):
    # One stuck connection must not stop the next message being delivered.
    # The single-threaded standard server does exactly that: a half-open
    # keep-alive socket left behind by a killed bot wedges it until restart.
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    port = int(os.environ.get("FAKE_EVOLUTION_PORT", "9999"))
    print(f"Fake Evolution API listening on http://localhost:{port}")
    print(f"Messages logged to {LOG_FILE}")
    Server(("0.0.0.0", port), Handler).serve_forever()
