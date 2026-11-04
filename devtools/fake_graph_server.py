# run with: python devtools/fake_graph_server.py
# set in .env before running Django against it: FB_GRAPH_BASE_URL=http://localhost:9000, FB_DIALOG_BASE_URL=http://localhost:9000, FB_APP_ID=123456, FB_APP_SECRET=fake-secret, IG_ENABLED=true
# FB_APP_ID must stay all-digits or config/settings.py raises ImproperlyConfigured
# never point production settings at this file

import http.server
import json
import os
import random
import re
import threading
import time
from urllib.parse import parse_qs, urlparse


container_poll_counts = {}
container_poll_lock = threading.Lock()


class FakeGraphHandler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _form(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        return parse_qs(body)

    def _forced_expiry(self, form):
        if form.get("access_token") == ["force-190"]:
            self._send_json(
                {"error": {"code": 190, "message": "Fake expired token"}},
                status=400,
            )
            return True
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        path = re.sub(r"^/v[0-9]+\.[0-9]+(?=/|$)", "", path)

        if path == "/dialog/oauth" or re.fullmatch(r"/v[0-9]+\.[0-9]+/dialog/oauth", path):
            redirect_uri = query.get("redirect_uri", [""])[0]
            state = query.get("state", [""])[0]
            self.send_response(302)
            self.send_header(
                "Location",
                f"{redirect_uri}?code=fake-oauth-code&state={state}",
            )
            self.end_headers()
            return

        if path == "/oauth/access_token":
            self._send_json({
                "access_token": f"fake-user-token-{random.randint(1000, 9999)}"
            })
            return

        if path == "/me/permissions":
            force_declined = "force_declined" in query
            permissions = [
                "pages_show_list",
                "pages_manage_posts",
                "instagram_content_publish",
            ]
            self._send_json({
                "data": [
                    {
                        "permission": name,
                        "status": (
                            "declined"
                            if force_declined and name == query["force_declined"][0]
                            else "granted"
                        ),
                    }
                    for name in permissions
                ]
            })
            return

        if path == "/me/accounts":
            if query.get("no_pages") == ["1"]:
                self._send_json({"data": []})
                return

            page = {
                "id": "fake-page-1",
                "name": "Fake Test Page",
                "access_token": f"fake-page-token-{random.randint(1000, 9999)}",
            }
            if query.get("no_ig") != ["1"]:
                page["instagram_business_account"] = {
                    "id": "fake-ig-1",
                    "username": "fake_ig_user",
                }
            self._send_json({"data": [page]})
            return

        if path == "/debug_token":
            self._send_json({
                "data": {
                    "is_valid": True,
                    "expires_at": int(time.time()) + 5184000,
                    "scopes": ["pages_show_list", "pages_manage_posts"],
                }
            })
            return

        item_match = re.fullmatch(r"/([^/]+)", path)
        if item_match:
            item_id = item_match.group(1)
            fields = query.get("fields", [""])[0]
            if fields == "permalink":
                self._send_json({"permalink": "https://www.instagram.com/p/fake/"})
                return
            if fields == "permalink_url":
                self._send_json({
                    "permalink_url": "https://www.facebook.com/posts/fake"
                })
                return
            if fields == "status_code":
                if "error" in item_id:
                    self._send_json({"status_code": "ERROR"})
                    return
                if "pending" in item_id:
                    with container_poll_lock:
                        call_count = container_poll_counts.get(item_id, 0) + 1
                        container_poll_counts[item_id] = call_count
                    status = "IN_PROGRESS" if call_count == 1 else "FINISHED"
                    self._send_json({"status_code": status})
                    return
                self._send_json({"status_code": "FINISHED"})
                return

        self._no_route()

    def do_POST(self):
        path = urlparse(self.path).path
        path = re.sub(r"^/v[0-9]+\.[0-9]+(?=/|$)", "", path)
        form = self._form()
        if re.fullmatch(r"/[^/]+/feed", path):
            if self._forced_expiry(form):
                return
            self._send_json({"id": f"fake-post-{random.randint(1000, 9999)}"})
            return

        if re.fullmatch(r"/[^/]+/media", path):
            if self._forced_expiry(form):
                return
            self._send_json({
                "id": f"fake-container-{random.randint(1000, 9999)}"
            })
            return

        if re.fullmatch(r"/[^/]+/media_publish", path):
            self._send_json({
                "id": f"fake-ig-media-{random.randint(1000, 9999)}"
            })
            return

        self._no_route()

    def _no_route(self):
        self._send_json(
            {"error": {"message": f"fake_graph_server: no route for {self.path}"}},
            status=404,
        )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "9000"))
    server = http.server.HTTPServer(("", port), FakeGraphHandler)
    print(f"Fake Graph API server listening on port {port}")
    server.serve_forever()
