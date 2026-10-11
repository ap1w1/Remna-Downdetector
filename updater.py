from __future__ import annotations

import hmac
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


UPDATE_TOKEN = os.environ.get("UPDATE_TOKEN", "")
UPDATE_COMMAND = os.environ.get(
    "UPDATE_COMMAND",
    "/workspace/update.sh",
)
update_lock = threading.Lock()


def run_update() -> None:
    try:
        subprocess.run(
            [UPDATE_COMMAND],
            cwd="/workspace",
            check=False,
            timeout=1800,
        )
    finally:
        update_lock.release()


class UpdateHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/update":
            self.send_error(404)
            return
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not UPDATE_TOKEN or not hmac.compare_digest(supplied, UPDATE_TOKEN):
            self.send_error(403)
            return
        if not update_lock.acquire(blocking=False):
            self.send_error(409, "Update is already running")
            return
        threading.Thread(target=run_update, daemon=True).start()
        body = json.dumps({"ok": True, "message": "Update started"}).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: object) -> None:
        print(format_string % args, flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8099), UpdateHandler).serve_forever()
