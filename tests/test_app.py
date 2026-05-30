import io
import json
import os
import tempfile
import time
import zipfile
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
        server.RECENT_FAILURES.clear()

    def wait_for_bulk_job(self, job_id):
        for _ in range(20):
            response = self.client.get(f"/api/bulk/jobs/{job_id}", headers=self.auth_headers)
            self.assertEqual(response.status_code, 200)
            job = response.json()["job"]
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.05)
        self.fail("Bulk job did not finish")

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

    def test_pptx_conversion_rewrites_slide_markers(self):
        original_engine = server.md_engine

        class StubEngine:
            def convert(self, path):
                return SimpleNamespace(
                    text_content="<!-- Slide number: 1 -->\nTitle\n\n<!-- Slide number: 2 -->\nBody"
                )

        server.md_engine = StubEngine()
        try:
            response = self.client.post(
                "/api/convert",
                headers=self.auth_headers,
                data={"engine": "standard"},
                files={"file": ("slides.pptx", b"deck", "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
            )
        finally:
            server.md_engine = original_engine

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("### Slide Number: 1\n\nTitle", payload["markdown"])
        self.assertIn("\n---\n\n### Slide Number: 2\n\nBody", payload["markdown"])
        self.assertNotIn("<!-- Slide number", payload["markdown"])

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

    def test_spreadsheet_conversion_returns_preview_and_authenticated_download(self):
        original_options = server.SPREADSHEET_OPTIONS
        original_artifact_dir = server.CONVERSION_ARTIFACT_DIR

        with tempfile.TemporaryDirectory() as tmp:
            server.CONVERSION_ARTIFACT_DIR = Path(tmp) / "artifacts"
            server.SPREADSHEET_OPTIONS = server.SpreadsheetConversionOptions(preview_rows_per_sheet=1)
            try:
                response = self.client.post(
                    "/api/convert",
                    headers=self.auth_headers,
                    data={"engine": "standard"},
                    files={
                        "file": (
                            "data.csv",
                            b"Name,Value\nAlice,1\nBob,2\n",
                            "text/csv",
                        )
                    },
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["converter"], "spreadsheet")
                self.assertTrue(payload["preview_truncated"])
                self.assertIn("download_id", payload)
                self.assertIn("## data", payload["markdown"])
                self.assertIn("Alice", payload["markdown"])
                self.assertNotIn("Bob", payload["markdown"])

                download_response = self.client.get(
                    f"/api/download/{payload['download_id']}",
                    headers=self.auth_headers,
                )

                self.assertEqual(download_response.status_code, 200)
                self.assertIn("| Bob | 2 |", download_response.text)
            finally:
                server.SPREADSHEET_OPTIONS = original_options
                server.CONVERSION_ARTIFACT_DIR = original_artifact_dir

    def test_diagnostics_requires_local_session_token(self):
        response = self.client.get("/api/diagnostics")

        self.assertEqual(response.status_code, 403)

    def test_diagnostics_returns_redacted_failure_packet(self):
        original_conversion_dir = server.CONVERSION_ARTIFACT_DIR
        original_bulk_dir = server.BULK_ARTIFACT_DIR
        original_jobs = server.bulk_jobs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server.CONVERSION_ARTIFACT_DIR = root / "artifacts"
            server.BULK_ARTIFACT_DIR = root / "bulk"
            now = time.time()
            server.bulk_jobs = {
                "queued": {"status": "queued", "created_at": now, "updated_at": now},
                "done": {"status": "completed", "created_at": now, "updated_at": now},
                "failed": {"status": "failed", "created_at": now, "updated_at": now},
            }
            server.remember_failure("conversion_failed", r"C:\private\source.xlsx failed", "source.xlsx")
            try:
                response = self.client.get("/api/diagnostics", headers=self.auth_headers)
            finally:
                server.CONVERSION_ARTIFACT_DIR = original_conversion_dir
                server.BULK_ARTIFACT_DIR = original_bulk_dir
                server.bulk_jobs = original_jobs

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        payload_text = json.dumps(payload)
        self.assertIn(payload["status"], {"ok", "degraded"})
        self.assertIn("bad_deployment", payload["failure_classes"])
        self.assertEqual(payload["bulk_jobs"]["tracked"], 3)
        self.assertEqual(payload["bulk_jobs"]["active"], 1)
        self.assertEqual(payload["recent_failures"][0]["category"], "conversion_failed")
        self.assertEqual(payload["recent_failures"][0]["source_extension"], ".xlsx")
        self.assertNotIn(str(root), payload_text)
        self.assertNotIn("C:\\private", payload_text)
        self.assertNotIn(server.APP_TOKEN, payload_text)

    def test_conversion_failure_is_available_in_diagnostics(self):
        original_engine = server.md_engine

        class FailingEngine:
            def convert(self, path):
                raise RuntimeError("engine exploded")

        server.md_engine = FailingEngine()
        try:
            response = self.client.post(
                "/api/convert",
                headers=self.auth_headers,
                data={"engine": "standard"},
                files={"file": ("sample.txt", b"hello", "text/plain")},
            )
            self.assertEqual(response.status_code, 500)

            diagnostics = self.client.get("/api/diagnostics", headers=self.auth_headers).json()
        finally:
            server.md_engine = original_engine

        self.assertEqual(diagnostics["recent_failures"][0]["category"], "conversion_failed")
        self.assertEqual(diagnostics["recent_failures"][0]["source_extension"], ".txt")
        self.assertIn("engine exploded", diagnostics["recent_failures"][0]["message"])

    def test_bulk_conversion_returns_manifest_and_authenticated_zip(self):
        original_engine = server.md_engine
        original_bulk_dir = server.BULK_ARTIFACT_DIR
        original_jobs = server.bulk_jobs

        class BulkEngine:
            def convert(self, path):
                source = Path(path)
                if source.name == "bad.txt":
                    raise RuntimeError("intentional failure")
                return SimpleNamespace(text_content=f"converted:{source.name}")

        with tempfile.TemporaryDirectory() as tmp:
            server.md_engine = BulkEngine()
            server.BULK_ARTIFACT_DIR = Path(tmp) / "bulk"
            server.bulk_jobs = {}
            try:
                response = self.client.post(
                    "/api/bulk/convert",
                    headers=self.auth_headers,
                    files=[
                        ("engine", (None, "standard")),
                        ("paths", (None, "folder/good.txt")),
                        ("paths", (None, "bad.txt")),
                        ("files", ("good.txt", b"hello", "text/plain")),
                        ("files", ("bad.txt", b"bad", "text/plain")),
                    ],
                )
                self.assertEqual(response.status_code, 202)
                job_id = response.json()["job"]["id"]

                job = self.wait_for_bulk_job(job_id)

                self.assertEqual(job["status"], "completed")
                self.assertEqual(job["summary"]["converted"], 1)
                self.assertEqual(job["summary"]["failed"], 1)
                self.assertIn("download_url", job)

                unauthenticated_download = self.client.get(job["download_url"])
                self.assertEqual(unauthenticated_download.status_code, 403)

                download = self.client.get(job["download_url"], headers=self.auth_headers)
                self.assertEqual(download.status_code, 200)
                with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                    names = set(archive.namelist())
                    self.assertIn("folder/good.md", names)
                    self.assertIn("batch-results.json", names)
                    self.assertEqual(archive.read("folder/good.md").decode("utf-8"), "converted:good.txt")
                    report = json.loads(archive.read("batch-results.json").decode("utf-8"))
                    self.assertFalse(report["success"])
                    self.assertEqual(report["failed"], 1)
            finally:
                server.md_engine = original_engine
                server.BULK_ARTIFACT_DIR = original_bulk_dir
                server.bulk_jobs = original_jobs

    def test_bulk_conversion_rejects_unsafe_paths(self):
        original_bulk_dir = server.BULK_ARTIFACT_DIR
        original_jobs = server.bulk_jobs

        with tempfile.TemporaryDirectory() as tmp:
            server.BULK_ARTIFACT_DIR = Path(tmp) / "bulk"
            server.bulk_jobs = {}
            try:
                response = self.client.post(
                    "/api/bulk/convert",
                    headers=self.auth_headers,
                    files=[
                        ("engine", (None, "standard")),
                        ("paths", (None, "../evil.txt")),
                        ("files", ("evil.txt", b"bad", "text/plain")),
                    ],
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("Invalid upload path", response.json()["detail"])
                self.assertEqual(server.bulk_jobs, {})
            finally:
                server.BULK_ARTIFACT_DIR = original_bulk_dir
                server.bulk_jobs = original_jobs

    def test_bulk_conversion_requires_local_session_token(self):
        response = self.client.post(
            "/api/bulk/convert",
            files=[
                ("engine", (None, "standard")),
                ("paths", (None, "sample.txt")),
                ("files", ("sample.txt", b"hello", "text/plain")),
            ],
        )

        self.assertEqual(response.status_code, 403)


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
        self.assertIn("!data.markdown_is_preview", app_js)

    def test_frontend_uses_download_artifacts_for_large_previews(self):
        app_js = Path("static/js/app.js").read_text(encoding="utf-8")

        self.assertIn("PREVIEW_RENDER_LIMIT", app_js)
        self.assertIn("currentDownloadId", app_js)
        self.assertIn("download_id", app_js)
        self.assertIn("preview_truncated", app_js)
        self.assertIn("/api/download/", app_js)

    def test_frontend_browse_button_and_fetch_hint_are_robust(self):
        index_html = Path("static/index.html").read_text(encoding="utf-8")
        style_css = Path("static/css/style.css").read_text(encoding="utf-8")
        app_js = Path("static/js/app.js").read_text(encoding="utf-8")

        self.assertIn('button type="button" class="browse-trigger" id="browse-trigger"', index_html)
        self.assertIn('aria-label="Choose a file to convert"', index_html)
        self.assertIn("showPicker", app_js)
        self.assertIn("fetchWithServerHint", app_js)
        self.assertIn("Cannot reach the local converter server", app_js)
        self.assertIn("pointer-events: none", style_css)
        self.assertNotIn("#file-input { display: none; }", style_css)

    def test_frontend_exposes_bulk_dashboard(self):
        index_html = Path("static/index.html").read_text(encoding="utf-8")
        app_js = Path("static/js/app.js").read_text(encoding="utf-8")
        style_css = Path("static/css/style.css").read_text(encoding="utf-8")

        self.assertIn('id="btn-bulk-view"', index_html)
        self.assertIn('id="bulk-workflow"', index_html)
        self.assertIn('id="bulk-folder-input"', index_html)
        self.assertIn("webkitdirectory", index_html)
        self.assertIn("API_BULK_CONVERT", app_js)
        self.assertIn("pollBulkJob", app_js)
        self.assertIn("downloadBulkZip", app_js)
        self.assertIn("paths", app_js)
        self.assertIn(".bulk-result-list", style_css)


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
