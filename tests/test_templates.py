import io
import json
import os
import tempfile
import unittest
from http.client import HTTPMessage

os.environ["TRACKING_DB"] = os.path.join(tempfile.mkdtemp(), "tracking-import.db")
os.environ["TRACKING_PAGE_TOKEN"] = "testtoken"
os.environ["TRACKING_REGISTER_KEY"] = "testkey"

import tracking  # noqa: E402


class FakeHandler:
    def __init__(self, method, path, body=b"", headers=None):
        self.command = method
        self.path = path
        self.headers = HTTPMessage()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        if body:
            self.headers["content-length"] = str(len(body))
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))

    def header(self, name):
        for key, value in self.response_headers:
            if key.lower() == name.lower():
                return value
        return None


class TemplateApiTest(unittest.TestCase):
    def setUp(self):
        tracking.DB_PATH = os.path.join(tempfile.mkdtemp(), "tracking.db")
        tracking.init_db()

    def call(self, method, path, body=None, headers=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        handler = FakeHandler(method, path, raw, headers or {})
        self.assertTrue(tracking.handle(handler, method))
        return handler

    def test_save_list_update_delete(self):
        key = {"X-Tracking-Key": "testkey", "Origin": "chrome-extension://abc"}
        saved = self.call(
            "POST",
            "/api/templates/save",
            {"name": "首次询盘", "html": "<p>Hi {{email}}</p>"},
            key,
        )
        self.assertEqual(saved.status, 200)
        body = saved.json()
        self.assertTrue(body["ok"])
        template = body["template"]
        self.assertTrue(template["id"].startswith("tpl_"))
        self.assertEqual(saved.header("Access-Control-Allow-Origin"), "chrome-extension://abc")

        listed = self.call("GET", "/api/templates?token=testtoken")
        self.assertEqual(listed.status, 200)
        self.assertEqual(len(listed.json()["templates"]), 1)
        self.assertEqual(listed.json()["templates"][0]["name"], "首次询盘")

        updated = self.call(
            "POST",
            "/api/templates/save",
            {"id": template["id"], "name": "报价跟进", "html": "<p>Follow up</p>"},
            key,
        )
        self.assertEqual(updated.status, 200)
        self.assertEqual(updated.json()["template"]["name"], "报价跟进")

        deleted = self.call(
            "POST", "/api/templates/delete", {"id": template["id"]}, key
        )
        self.assertEqual(deleted.status, 200)
        self.assertTrue(deleted.json()["ok"])
        self.assertEqual(tracking.list_templates(), [])

    def test_rejects_bad_input_and_unauthorized(self):
        no_auth = self.call("GET", "/api/templates")
        self.assertEqual(no_auth.status, 401)

        blank = self.call(
            "POST",
            "/api/templates/save",
            {"name": "", "html": ""},
            {"X-Tracking-Key": "testkey"},
        )
        self.assertEqual(blank.status, 400)
        self.assertFalse(blank.json()["ok"])

    def test_options_returns_cors(self):
        handler = self.call(
            "OPTIONS",
            "/api/templates",
            headers={"Origin": "https://mail.zoho.com.cn"},
        )
        self.assertEqual(handler.status, 204)
        self.assertEqual(handler.header("Access-Control-Allow-Origin"), "https://mail.zoho.com.cn")

    def test_templates_survive_reinit(self):
        self.call(
            "POST",
            "/api/templates/save",
            {"name": "Keep", "html": "<p>keep</p>"},
            {"X-Tracking-Key": "testkey"},
        )
        tracking.init_db()
        self.assertEqual(len(tracking.list_templates()), 1)


if __name__ == "__main__":
    unittest.main()
