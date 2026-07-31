import hashlib
import json
import tempfile
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pipeline_distill


class StubMarkItDown:
    def __init__(self, text="manual text"):
        self.text = text

    def convert(self, path):
        return SimpleNamespace(text_content=self.text)


def stub_layout(source, target):
    source_hash = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    payload = {
        "schema_version": "opics.deepdoc-ocr.v1",
        "parser": "deepdoc-ocr",
        "source_sha256": source_hash,
        "page_count": 1,
        "selected_pages": [1],
        "selection_truncated": False,
        "runtime": {"fixture": True},
        "pages": [
            {
                "page": 1,
                "ocr_text": "OCR manual text",
                "boxes": [
                    {
                        "text": "OCR manual text",
                        "confidence": 0.99,
                        "x0": 0.1,
                        "x1": 0.5,
                        "y0": 0.1,
                        "y1": 0.2,
                    }
                ],
            }
        ],
    }
    target.write_text(json.dumps(payload), encoding="utf-8")
    return payload


class PipelineDistillTests(unittest.TestCase):
    def test_caller_owned_source_survives_and_receipt_has_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"manual")
            output = root / "artifacts"

            result = pipeline_distill.distill_file(
                source,
                output,
                source_uri="https://oem.example/manual.pdf",
                parser="auto",
                markitdown_engine=StubMarkItDown(),
                layout_runner=stub_layout,
            )

            expected_hash = hashlib.sha256(b"manual").hexdigest()
            self.assertTrue(source.exists())
            self.assertEqual(result.parser_used, "outpace-markitdown+deepdoc-ocr")
            receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
            self.assertEqual(receipt["source"]["sha256"], expected_hash)
            self.assertEqual(receipt["source"]["uri"], "https://oem.example/manual.pdf")
            self.assertTrue(
                receipt["document_provenance"]["source_locator_present"]
            )
            self.assertTrue(receipt["eligible_for_schema_extraction_attempt"])
            parser = receipt["parser"]
            self.assertIn(
                parser["owner_worktree_state"],
                {"clean", "dirty", "unresolved"},
            )
            self.assertTrue(parser["owner_code_sha256"]["pipeline_distill.py"])
            if parser["owner_worktree_state"] == "clean":
                self.assertEqual(
                    parser["owner_commit"],
                    parser["owner_head_commit"],
                )
            else:
                self.assertIsNone(parser["owner_commit"])
            self.assertFalse(receipt["eligible_for_complete_row_promotion"])
            self.assertEqual(
                receipt["cleanup"]["retention_policy"], "SOURCE_OF_RECORD_KEEP"
            )
            self.assertIsNotNone(receipt["outputs"]["layout"])

    def test_pipeline_owned_source_moves_to_recoverable_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = root / "intake"
            intake.mkdir()
            source = intake / "brochure.txt"
            source.write_text("brochure", encoding="utf-8")
            output = root / "artifacts"

            result = pipeline_distill.distill_file(
                source,
                output,
                source_uri="https://oem.example/brochure.txt",
                parser="markitdown",
                owned_root=intake,
                markitdown_engine=StubMarkItDown(),
            )

            self.assertFalse(source.exists())
            self.assertEqual(result.raw_disposition, "QUARANTINED")
            receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
            self.assertTrue(receipt["cleanup"]["custody_action_performed"])
            self.assertFalse(receipt["cleanup"]["raw_bytes_deleted"])
            quarantine = intake / receipt["cleanup"]["quarantine_name"]
            self.assertEqual(quarantine.read_text(encoding="utf-8"), "brochure")
            self.assertTrue(Path(result.markdown_path).exists())

    def test_failed_conversion_never_deletes_pipeline_owned_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = root / "intake"
            intake.mkdir()
            source = intake / "empty.txt"
            source.write_text("source", encoding="utf-8")

            with self.assertRaises(pipeline_distill.DistillationError):
                pipeline_distill.distill_file(
                    source,
                    root / "artifacts",
                    owned_root=intake,
                    parser="markitdown",
                    markitdown_engine=StubMarkItDown(text=""),
                )

            self.assertTrue(source.exists())

    def test_final_receipt_failure_leaves_raw_bytes_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = root / "intake"
            intake.mkdir()
            source = intake / "manual.txt"
            source.write_text("manual", encoding="utf-8")
            original_writer = pipeline_distill._write_json_atomic
            calls = 0

            def fail_second_receipt(path, payload):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated disk failure")
                return original_writer(path, payload)

            with patch.object(
                pipeline_distill,
                "_write_json_atomic",
                side_effect=fail_second_receipt,
            ):
                with self.assertRaises(OSError):
                    pipeline_distill.distill_file(
                        source,
                        root / "artifacts",
                        source_uri="https://oem.example/manual.txt",
                        parser="markitdown",
                        owned_root=intake,
                        markitdown_engine=StubMarkItDown(),
                    )

            quarantined = list((intake / ".opics-distill-quarantine").glob("*.pending"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "manual")
            self.assertEqual(
                len(list((root / "artifacts").glob("*.receipt.json"))),
                1,
            )

    def test_owned_root_cannot_cover_output_or_unrelated_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = root / "intake"
            intake.mkdir()
            source = root / "manual.txt"
            source.write_text("manual", encoding="utf-8")

            with self.assertRaises(pipeline_distill.DistillationError):
                pipeline_distill.distill_file(
                    source,
                    root / "artifacts",
                    owned_root=intake,
                    markitdown_engine=StubMarkItDown(),
                )

            inside = intake / "manual.txt"
            inside.write_text("manual", encoding="utf-8")
            with self.assertRaises(pipeline_distill.DistillationError):
                pipeline_distill.distill_file(
                    inside,
                    intake / "artifacts",
                    owned_root=intake,
                    markitdown_engine=StubMarkItDown(),
                )

    def test_auto_falls_back_but_deepdoc_mode_fails_closed(self):
        def broken_layout(source, target):
            raise RuntimeError("layout unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"manual")

            result = pipeline_distill.distill_file(
                source,
                root / "auto",
                source_uri="https://oem.example/manual.pdf",
                parser="auto",
                markitdown_engine=StubMarkItDown(),
                layout_runner=broken_layout,
            )
            self.assertEqual(result.parser_used, "outpace-markitdown")
            self.assertIn("DeepDoc layout failed", result.warnings[0])

            with self.assertRaises(pipeline_distill.DistillationError):
                pipeline_distill.distill_file(
                    source,
                    root / "required",
                    parser="deepdoc",
                    markitdown_engine=StubMarkItDown(),
                    layout_runner=broken_layout,
                )

    def test_layout_artifact_is_canonical_not_runner_return_value(self):
        def split_layout(source, target):
            canonical = stub_layout(source, target)
            returned = dict(canonical)
            returned["pages"] = [
                {
                    **canonical["pages"][0],
                    "ocr_text": "RETURNED BUT NOT RECEIPTED",
                }
            ]
            return returned

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.pdf"
            source.write_bytes(b"scan")
            result = pipeline_distill.distill_file(
                source,
                root / "artifacts",
                source_uri="https://oem.example/scan.pdf",
                parser="auto",
                markitdown_engine=StubMarkItDown(text=""),
                layout_runner=split_layout,
            )
            markdown = Path(result.markdown_path).read_text(encoding="utf-8")
            self.assertIn("OCR manual text", markdown)
            self.assertNotIn("RETURNED BUT NOT RECEIPTED", markdown)

    def test_wrong_layout_source_hash_fails_closed(self):
        def wrong_hash(source, target):
            payload = stub_layout(source, target)
            payload["source_sha256"] = "0" * 64
            target.write_text(json.dumps(payload), encoding="utf-8")
            return payload

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.pdf"
            source.write_bytes(b"scan")
            with self.assertRaises(pipeline_distill.DistillationError):
                pipeline_distill.distill_file(
                    source,
                    root / "artifacts",
                    source_uri="https://oem.example/scan.pdf",
                    parser="deepdoc",
                    markitdown_engine=StubMarkItDown(text=""),
                    layout_runner=wrong_hash,
                )

    def test_deepdoc_supplies_markdown_when_markitdown_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.pdf"
            source.write_bytes(b"scan")

            result = pipeline_distill.distill_file(
                source,
                root / "artifacts",
                source_uri="https://oem.example/scan.pdf",
                parser="auto",
                markitdown_engine=StubMarkItDown(text=""),
                layout_runner=stub_layout,
            )

            self.assertEqual(result.parser_used, "deepdoc-ocr")
            markdown = Path(result.markdown_path).read_text(encoding="utf-8")
            self.assertIn("## Page 1", markdown)
            self.assertIn("OCR manual text", markdown)
            self.assertIn("DeepDoc OCR supplied Markdown", result.warnings[0])

    def test_partial_deepdoc_fallback_is_not_schema_extraction_eligible(self):
        def partial_layout(source, target):
            payload = stub_layout(source, target)
            payload["page_count"] = 10
            payload["selection_truncated"] = True
            target.write_text(json.dumps(payload), encoding="utf-8")
            return payload

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.pdf"
            source.write_bytes(b"scan")
            result = pipeline_distill.distill_file(
                source,
                root / "artifacts",
                source_uri="https://oem.example/scan.pdf",
                parser="auto",
                markitdown_engine=StubMarkItDown(text=""),
                layout_runner=partial_layout,
            )
            receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "distilled_partial")
            self.assertEqual(receipt["coverage"]["content_scope"], "selected_pages")
            self.assertTrue(receipt["eligible_for_schema_extraction_attempt"])
            self.assertFalse(receipt["document_scope_complete"])

            fragment = pipeline_distill.distill_file(
                source,
                root / "fragment",
                source_uri="https://oem.example/scan.pdf",
                parser="auto",
                markitdown_engine=StubMarkItDown(text="Title"),
                layout_runner=partial_layout,
            )
            fragment_receipt = json.loads(
                Path(fragment.receipt_path).read_text(encoding="utf-8")
            )
            self.assertEqual(
                fragment_receipt["coverage"]["content_scope"],
                "selected_pages",
            )
            self.assertTrue(
                fragment_receipt["eligible_for_schema_extraction_attempt"]
            )
            self.assertFalse(fragment_receipt["document_scope_complete"])

    def test_pdf_text_quality_is_not_falsely_treated_as_page_coverage(self):
        def should_not_run(source, target):
            raise AssertionError("quality router should not invoke DeepDoc")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"manual")
            result = pipeline_distill.distill_file(
                source,
                root / "artifacts",
                source_uri="https://oem.example/manual.pdf",
                parser="auto",
                markitdown_engine=StubMarkItDown(
                    text=("Title and copyright " * 25)
                ),
                layout_runner=should_not_run,
            )
            receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
            self.assertEqual(receipt["coverage"]["content_scope"], "unknown")
            self.assertTrue(
                receipt["coverage"][
                    "text_quality_sufficient_for_extraction_attempt"
                ]
            )
            self.assertTrue(receipt["eligible_for_schema_extraction_attempt"])
            self.assertFalse(receipt["document_scope_complete"])

    def test_same_content_different_sources_keep_distinct_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.txt"
            source.write_text("same bytes", encoding="utf-8")
            first = pipeline_distill.distill_file(
                source,
                root / "artifacts",
                source_uri="https://one.example/manual.txt",
                parser="markitdown",
                markitdown_engine=StubMarkItDown(),
            )
            second = pipeline_distill.distill_file(
                source,
                root / "artifacts",
                source_uri="https://two.example/manual.txt",
                parser="markitdown",
                markitdown_engine=StubMarkItDown(),
            )

            self.assertEqual(first.artifact_id, second.artifact_id)
            self.assertNotEqual(first.receipt_path, second.receipt_path)
            receipts = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (root / "artifacts").glob("*.receipt.json")
            ]
            self.assertEqual(len(receipts), 2)
            self.assertEqual(
                len({receipt["observation_id"] for receipt in receipts}),
                2,
            )

    def test_different_converter_outputs_never_rewrite_old_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.txt"
            source.write_text("same source", encoding="utf-8")
            first = pipeline_distill.distill_file(
                source,
                root / "artifacts",
                source_uri="https://oem.example/manual.txt",
                parser="markitdown",
                markitdown_engine=StubMarkItDown(text="output A"),
            )
            first_bytes = Path(first.markdown_path).read_bytes()
            second = pipeline_distill.distill_file(
                source,
                root / "artifacts",
                source_uri="https://oem.example/manual.txt",
                parser="markitdown",
                markitdown_engine=StubMarkItDown(text="output B"),
            )

            self.assertNotEqual(first.artifact_id, second.artifact_id)
            self.assertEqual(Path(first.markdown_path).read_bytes(), first_bytes)
            first_receipt = json.loads(
                Path(first.receipt_path).read_text(encoding="utf-8")
            )
            self.assertEqual(
                hashlib.sha256(first_bytes).hexdigest(),
                first_receipt["outputs"]["markdown"]["sha256"],
            )

    def test_changed_owned_source_is_not_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = root / "intake"
            intake.mkdir()
            source = intake / "manual.txt"
            source.write_text("AAAA", encoding="utf-8")

            class MutatingEngine:
                def convert(self, path):
                    source.write_text("BBBB", encoding="utf-8")
                    return SimpleNamespace(text_content="manual")

            result = pipeline_distill.distill_file(
                source,
                root / "artifacts",
                source_uri="https://oem.example/manual.txt",
                parser="markitdown",
                owned_root=intake,
                markitdown_engine=MutatingEngine(),
            )
            self.assertEqual(result.raw_disposition, "RETAINED")
            self.assertEqual(source.read_text(encoding="utf-8"), "BBBB")
            receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
            self.assertEqual(receipt["cleanup"]["disposition"], "RETAINED")
            self.assertIn("source identity changed", receipt["cleanup"]["detail"])

    def test_quarantine_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = root / "intake"
            outside = root / "outside"
            intake.mkdir()
            outside.mkdir()
            (intake / ".opics-distill-quarantine").symlink_to(
                outside,
                target_is_directory=True,
            )
            source = intake / "manual.txt"
            source.write_text("manual", encoding="utf-8")
            result = pipeline_distill.distill_file(
                source,
                root / "artifacts",
                source_uri="https://oem.example/manual.txt",
                parser="markitdown",
                owned_root=intake,
                markitdown_engine=StubMarkItDown(),
            )
            self.assertEqual(result.raw_disposition, "RETAINED")
            self.assertTrue(source.exists())
            self.assertEqual(list(outside.iterdir()), [])

    def test_mixed_archive_never_claims_full_document_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "mixed.zip"
            output = root / "artifacts"
            with zipfile.ZipFile(
                source,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr("good.txt", "supported public manual text")
                archive.writestr("opaque.xyz", b"\x00\x01\x02unsupported")

            result = pipeline_distill.distill_file(
                source,
                output,
                source_uri="https://oem.example/mixed.zip",
                parser="markitdown",
            )
            receipt = json.loads(
                Path(result.receipt_path).read_text(encoding="utf-8")
            )
            self.assertEqual(
                receipt["coverage"]["content_scope"],
                "parser_output_only",
            )
            self.assertFalse(receipt["document_scope_complete"])
            self.assertEqual(receipt["status"], "distilled_partial")

    def test_parallel_owned_source_has_one_custody_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = root / "intake"
            output = root / "artifacts"
            intake.mkdir()
            source = intake / "manual.txt"
            source.write_text("manual", encoding="utf-8")

            class SlowEngine:
                def convert(self, path):
                    time.sleep(0.1)
                    return SimpleNamespace(text_content="manual text")

            def invoke():
                return pipeline_distill.distill_file(
                    source,
                    output,
                    source_uri="https://oem.example/manual.txt",
                    parser="markitdown",
                    owned_root=intake,
                    markitdown_engine=SlowEngine(),
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(invoke) for _ in range(2)]
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except pipeline_distill.DistillationError as exc:
                        outcomes.append(exc)

            winners = [
                item for item in outcomes
                if isinstance(item, pipeline_distill.DistillationResult)
            ]
            refusals = [
                item for item in outcomes
                if isinstance(item, pipeline_distill.DistillationError)
            ]
            self.assertEqual(len(winners), 1)
            self.assertEqual(winners[0].raw_disposition, "QUARANTINED")
            self.assertEqual(len(refusals), 1)
            self.assertIn("already be quarantined", str(refusals[0]))
            self.assertFalse(source.exists())
            self.assertEqual(
                len(list((intake / ".opics-distill-quarantine").iterdir())),
                1,
            )


if __name__ == "__main__":
    unittest.main()
