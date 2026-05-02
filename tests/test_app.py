import os
from pathlib import Path
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

os.environ.setdefault("APP_NAME", "TestMarkItDown")
os.environ.setdefault("APP_HOST", "127.0.0.1")
os.environ.setdefault("APP_PORT", "8765")
os.environ.setdefault("APP_RELOAD", "0")
os.environ.setdefault("APP_TOKEN", "test-local-token")

import server


class ServerBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.auth_headers = {"X-MD-Creator-Token": server.APP_TOKEN}

    def test_health_reports_runtime_configuration(self):
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["app_name"], server.APP_NAME)
        self.assertEqual(payload["host"], server.APP_HOST)
        self.assertEqual(payload["port"], server.APP_PORT)
        self.assertTrue(payload["engines"]["standard"])

    def test_frontend_receives_runtime_token(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(server.APP_TOKEN, response.text)
        self.assertNotIn("__MD_CREATOR_TOKEN__", response.text)

    def test_convert_streams_file_and_normalizes_unknown_engine(self):
        original_engine = server.md_engine

        class StubEngine:
            def convert(self, path):
                self.seen_size = Path(path).stat().st_size
                return SimpleNamespace(text_content="converted text")

        stub_engine = StubEngine()
        server.md_engine = stub_engine
        try:
            response = self.client.post(
                "/api/convert",
                headers=self.auth_headers,
                data={"engine": "unexpected"},
                files={"file": ("sample.txt", b"hello", "text/plain")},
            )
        finally:
            server.md_engine = original_engine

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["file_size"], 5)
        self.assertEqual(payload["engine"], "standard")
        self.assertEqual(payload["markdown"], "converted text")
        self.assertEqual(stub_engine.seen_size, 5)

    def test_convert_requires_local_session_token(self):
        response = self.client.post(
            "/api/convert",
            data={"engine": "standard"},
            files={"file": ("sample.txt", b"hello", "text/plain")},
        )

        self.assertEqual(response.status_code, 403)

    def test_rejects_oversized_request_before_conversion(self):
        original_limit = server.MAX_REQUEST_SIZE
        original_engine = server.md_engine

        class FailingEngine:
            def convert(self, path):
                raise AssertionError("conversion should not run")

        server.MAX_REQUEST_SIZE = 10
        server.md_engine = FailingEngine()
        try:
            response = self.client.post(
                "/api/convert",
                headers=self.auth_headers,
                data={"engine": "standard"},
                files={"file": ("sample.txt", b"hello", "text/plain")},
            )
        finally:
            server.MAX_REQUEST_SIZE = original_limit
            server.md_engine = original_engine

        self.assertEqual(response.status_code, 413)
        self.assertIn("Request too large", response.json()["detail"])


class FrontendGuardTests(unittest.TestCase):
    def test_frontend_uses_local_token_and_vendored_markdown_it(self):
        index_html = Path("static/index.html").read_text(encoding="utf-8")
        app_js = Path("static/js/app.js").read_text(encoding="utf-8")

        self.assertIn('name="mdcreator-token"', index_html)
        self.assertIn('/static/js/markdown-it.min.js', index_html)
        self.assertNotIn("cdn.jsdelivr.net", index_html)
        self.assertIn("X-MD-Creator-Token", app_js)
        self.assertTrue(Path("static/js/markdown-it.min.js").exists())

    def test_preview_raw_html_is_disabled(self):
        app_js = Path("static/js/app.js").read_text(encoding="utf-8")

        self.assertIn("html: false", app_js)
        self.assertNotIn("html: true", app_js)

    def test_large_history_entries_are_omitted_safely(self):
        app_js = Path("static/js/app.js").read_text(encoding="utf-8")

        self.assertIn("HISTORY_MARKDOWN_LIMIT", app_js)
        self.assertIn("markdown_omitted", app_js)
        self.assertIn("History save failed", app_js)


class LauncherGuardTests(unittest.TestCase):
    def test_start_script_is_locked_and_idempotent(self):
        start_script = Path("start.bat").read_text(encoding="utf-8")

        self.assertIn('set "LOCK_DIR=.start.lock"', start_script)
        self.assertIn("start is already in progress", start_script)
        self.assertIn("is already running and open at %APP_URL%", start_script)
        self.assertIn("Start-Process -FilePath $env:APP_URL", start_script)
        self.assertNotIn("No new browser window was opened.", start_script)
        self.assertIn("call stop.bat /quiet", start_script)
        self.assertIn('del /f /q "%PID_FILE%"', start_script)

    def test_stop_script_suppresses_stale_taskkill_races(self):
        stop_script = Path("stop.bat").read_text(encoding="utf-8")

        self.assertIn("*> $null", stop_script)
        self.assertIn("No %APP_NAME% instance was running.", stop_script)


if __name__ == "__main__":
    unittest.main()
