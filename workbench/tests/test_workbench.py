"""Regression tests for the local bridge, using a fake publishing backend only."""

import base64
import json
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from workbench.server import APIError, Backend, LocalStore, WorkbenchServer, title_length
from workbench.run import runtime_path

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7L8AAAAASUVORK5CYII=")


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.logged_in = True
        self.fail_publish = False

    def call(self, path, payload=None, timeout=90):
        self.calls.append((path, payload))
        if path == "/api/v1/login/status":
            return {"success": True, "data": {"is_logged_in": self.logged_in, "username": "测试账号"}}
        if path == "/api/v1/publish" and self.fail_publish:
            raise APIError("timeout", 502)
        return {"success": True, "data": {"status": "submitted"}}


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = LocalStore(self.directory.name)
        self.backend = FakeBackend()
        self.server = WorkbenchServer(("127.0.0.1", 0), self.store, self.backend,
                                      Path(__file__).resolve().parents[1] / "web")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))
        self.client.open(self.base + "/").close()
        asset = self.store.upload({"filename": "照片.png", "base64": base64.b64encode(PNG).decode()})
        self.draft = {"id": str(uuid.uuid4()), "title": "测试标题", "content": "这是一份测试草稿。",
                      "tags": ["测试"], "images": [asset], "visibility": "private", "is_original": False}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, path, payload=None, method=None, headers=None, client=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, data=data, method=method,
                          headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = (client or self.client).open(request)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_draft_round_trip_upsert_and_delete(self):
        for title in ("第一版", "第二版"):
            self.draft["title"] = title
            code, _ = self.request("/api/drafts", self.draft)
            self.assertEqual(code, 200)
        code, result = self.request("/api/drafts")
        self.assertEqual(code, 200)
        self.assertEqual(len(result["data"]["drafts"]), 1)
        self.assertEqual(result["data"]["drafts"][0]["title"], "第二版")
        reloaded = LocalStore(self.directory.name)
        self.assertEqual(reloaded.list()[0]["id"], self.draft["id"])
        code, _ = self.request("/api/drafts/" + self.draft["id"], method="DELETE")
        self.assertEqual(code, 200)
        self.assertEqual(self.store.list(), [])

    def test_title_count_matches_utf16_rule(self):
        self.assertEqual(title_length("测试ABC😀"), 6)
        self.assertEqual(title_length("a" * 40), 20)
        self.assertEqual(title_length("😀" * 10), 20)
        self.draft["title"] = "字" * 21
        with self.assertRaises(APIError):
            self.store.prepare(self.draft)

    def test_schedule_and_visibility_mapping(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        self.draft["schedule_at"] = (now + timedelta(hours=2)).isoformat()
        prepared = self.store.prepare(self.draft, now)
        self.assertEqual(prepared["visibility"], "仅自己可见")
        self.assertTrue(Path(prepared["images"][0]).is_absolute())
        for invalid in ((now + timedelta(minutes=30)).isoformat(), (now + timedelta(days=15)).isoformat(), "2026-10-07T23:00:00"):
            self.draft["schedule_at"] = invalid
            with self.assertRaises(APIError):
                self.store.prepare(self.draft, now)

    def test_image_content_and_paths_are_checked(self):
        with self.assertRaises(APIError):
            self.store.upload({"filename": "pretend.png", "base64": base64.b64encode(b"<script>bad</script>").decode()})
        self.draft["images"] = [{"id": "../../cookies.json", "name": "bad"}]
        with self.assertRaises(APIError):
            self.store.prepare(self.draft)
        code, _ = self.request("/assets/../../backend-token")
        self.assertEqual(code, 404)

    def test_local_session_and_origin_required(self):
        code, _ = self.request("/api/drafts", client=build_opener())
        self.assertEqual(code, 401)
        code, _ = self.request("/api/drafts", self.draft, headers={"Origin": "https://example.com"})
        self.assertEqual(code, 403)
        code, _ = self.request("/api/drafts", headers={"Host": "evil.example"})
        self.assertEqual(code, 403)

    def test_confirmation_snapshot_and_duplicate_submission(self):
        _, review = self.request("/api/prepare", self.draft)
        publish = dict(self.draft, confirmed=True, confirmation_id=review["data"]["confirmation_id"])
        code, _ = self.request("/api/publish", publish)
        self.assertEqual(code, 200)
        code, _ = self.request("/api/publish", publish)
        self.assertEqual(code, 409)
        calls = [item for item in self.backend.calls if item[0] == "/api/v1/publish"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["visibility"], "仅自己可见")

    def test_changed_preview_and_unconfirmed_call_cannot_publish(self):
        code, _ = self.request("/api/publish", self.draft)
        self.assertEqual(code, 400)
        _, review = self.request("/api/prepare", self.draft)
        changed = dict(self.draft, title="改了标题", confirmed=True, confirmation_id=review["data"]["confirmation_id"])
        code, _ = self.request("/api/publish", changed)
        self.assertEqual(code, 409)
        self.assertFalse(any(item[0] == "/api/v1/publish" for item in self.backend.calls))

    def test_not_logged_in_cannot_publish(self):
        self.backend.logged_in = False
        _, review = self.request("/api/prepare", self.draft)
        code, result = self.request("/api/publish", dict(self.draft, confirmed=True, confirmation_id=review["data"]["confirmation_id"]))
        self.assertEqual(code, 409)
        self.assertIn("登录", result["error"])
        self.assertFalse(any(item[0] == "/api/v1/publish" for item in self.backend.calls))

    def test_failed_submission_is_not_retried(self):
        self.backend.fail_publish = True
        _, review = self.request("/api/prepare", self.draft)
        payload = dict(self.draft, confirmed=True, confirmation_id=review["data"]["confirmation_id"])
        code, result = self.request("/api/publish", payload)
        self.assertEqual(code, 502)
        self.assertIn("结果未知", result["error"])
        self.assertEqual(self.request("/api/publish", payload)[0], 409)
        self.assertEqual(len([item for item in self.backend.calls if item[0] == "/api/v1/publish"]), 1)

    def test_other_ticket_and_restart_cannot_repeat_content(self):
        _, first = self.request("/api/prepare", self.draft)
        _, second = self.request("/api/prepare", self.draft)
        payload = dict(self.draft, confirmed=True, confirmation_id=first["data"]["confirmation_id"])
        self.assertEqual(self.request("/api/publish", payload)[0], 200)
        payload["confirmation_id"] = second["data"]["confirmation_id"]
        self.assertEqual(self.request("/api/publish", payload)[0], 409)
        restarted = WorkbenchServer(("127.0.0.1", 0), LocalStore(self.directory.name), self.backend,
                                    Path(__file__).resolve().parents[1] / "web")
        try:
            with self.assertRaises(APIError) as caught:
                restarted.review(self.draft)
            self.assertEqual(caught.exception.code, "ALREADY_SUBMITTED")
        finally:
            restarted.server_close()
        self.assertEqual(len([item for item in self.backend.calls if item[0] == "/api/v1/publish"]), 1)

    def test_unknown_requires_explicit_check_before_unlock(self):
        self.backend.fail_publish = True
        _, review = self.request("/api/prepare", self.draft)
        self.request("/api/publish", dict(self.draft, confirmed=True, confirmation_id=review["data"]["confirmation_id"]))
        self.assertEqual(self.request("/api/prepare", self.draft)[1]["code"], "PUBLISH_RESULT_UNKNOWN")
        self.assertEqual(self.request("/api/publish/reset", self.draft)[0], 400)
        self.assertEqual(self.request("/api/publish/reset", dict(self.draft, confirmed_not_published=True))[0], 200)
        self.backend.fail_publish = False
        _, new_review = self.request("/api/prepare", self.draft)
        self.assertEqual(self.request("/api/publish", dict(self.draft, confirmed=True, confirmation_id=new_review["data"]["confirmation_id"]))[0], 200)
        self.assertEqual(self.request("/api/publish/reset", dict(self.draft, confirmed_not_published=True))[0], 409)

    def test_custom_runtime_cannot_put_secrets_into_checkout(self):
        with self.assertRaises(ValueError):
            runtime_path(Path(__file__).resolve().parents[1] / "custom-runtime")
        self.assertEqual(runtime_path(self.directory.name), Path(self.directory.name).resolve())

    def test_backend_must_be_local(self):
        for url in ("https://127.0.0.1:18060", "http://example.com", "http://localhost:18060/api", "http://user:secret@localhost"):
            with self.assertRaises(ValueError):
                Backend(url)


if __name__ == "__main__":
    unittest.main()
