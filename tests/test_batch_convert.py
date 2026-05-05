import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import batch_convert


class StubMarkItDown:
    def __init__(self):
        self.converted = []

    def convert(self, path):
        source = Path(path)
        self.converted.append(source.name)
        return SimpleNamespace(text_content=f"standard:{source.name}")


class StubMinerU:
    def __init__(self):
        self.converted = []

    def flash_extract(self, path):
        source = Path(path)
        self.converted.append(source.name)
        return SimpleNamespace(markdown=f"academic:{source.name}")


class BatchConvertTests(unittest.TestCase):
    def test_batch_bat_uses_folder_defaults_and_academic_mode(self):
        batch_script = Path("batch.bat").read_text(encoding="utf-8")

        self.assertIn('if "%INPUT_DIR%"=="" set "INPUT_DIR=input"', batch_script)
        self.assertIn('if "%OUTPUT_DIR%"=="" set "OUTPUT_DIR=output"', batch_script)
        self.assertIn('batch_convert.py" "%INPUT_DIR%" "%OUTPUT_DIR%" --engine academic', batch_script)

    def test_standard_batch_converts_tree_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            (input_dir / "nested").mkdir(parents=True)
            (input_dir / "notes.txt").write_text("hello", encoding="utf-8")
            (input_dir / "nested" / "slides.pptx").write_text("deck", encoding="utf-8")

            summary = batch_convert.run_batch(
                input_dir,
                output_dir,
                engine="standard",
                md_engine=StubMarkItDown(),
            )

            self.assertEqual(summary.converted, 2)
            self.assertEqual(summary.failed, 0)
            self.assertEqual((output_dir / "notes.md").read_text(encoding="utf-8"), "standard:notes.txt")
            self.assertEqual(
                (output_dir / "nested" / "slides.md").read_text(encoding="utf-8"),
                "standard:slides.pptx",
            )

            report = json.loads((output_dir / "batch-results.json").read_text(encoding="utf-8"))
            self.assertTrue(report["success"])
            self.assertEqual(report["converted"], 2)
            self.assertEqual({item["source"] for item in report["results"]}, {"notes.txt", "nested/slides.pptx"})

    def test_academic_batch_uses_mineru_for_pdfs_and_standard_for_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "paper.pdf").write_text("pdf", encoding="utf-8")
            (input_dir / "notes.txt").write_text("txt", encoding="utf-8")
            md_engine = StubMarkItDown()
            mineru_client = StubMinerU()

            summary = batch_convert.run_batch(
                input_dir,
                output_dir,
                engine="academic",
                md_engine=md_engine,
                mineru_client=mineru_client,
            )

            self.assertEqual(summary.converted, 2)
            self.assertEqual((output_dir / "paper.md").read_text(encoding="utf-8"), "academic:paper.pdf")
            self.assertEqual((output_dir / "notes.md").read_text(encoding="utf-8"), "standard:notes.txt")
            self.assertEqual(mineru_client.converted, ["paper.pdf"])
            self.assertEqual(md_engine.converted, ["notes.txt"])
            notes_result = next(item for item in summary.results if item.source == "notes.txt")
            self.assertEqual(notes_result.engine_used, "standard")
            self.assertIn("Academic engine only supports PDF", notes_result.warning)

    def test_academic_batch_checks_missing_mineru_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "first.pdf").write_text("pdf", encoding="utf-8")
            (input_dir / "second.pdf").write_text("pdf", encoding="utf-8")

            with patch.object(batch_convert, "_build_mineru", return_value=None) as build_mineru:
                summary = batch_convert.run_batch(
                    input_dir,
                    output_dir,
                    engine="academic",
                    md_engine=StubMarkItDown(),
                )

            self.assertEqual(summary.converted, 2)
            self.assertEqual(build_mineru.call_count, 1)
            self.assertTrue(all(item.engine_used == "standard" for item in summary.results))

    def test_existing_output_is_skipped_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            (input_dir / "notes.txt").write_text("new", encoding="utf-8")
            (output_dir / "notes.md").write_text("old", encoding="utf-8")
            md_engine = StubMarkItDown()

            summary = batch_convert.run_batch(
                input_dir,
                output_dir,
                engine="standard",
                md_engine=md_engine,
            )

            self.assertEqual(summary.skipped, 1)
            self.assertEqual(summary.converted, 0)
            self.assertEqual(md_engine.converted, [])
            self.assertEqual((output_dir / "notes.md").read_text(encoding="utf-8"), "old")

    def test_reports_output_name_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "paper.docx").write_text("doc", encoding="utf-8")
            (input_dir / "paper.pdf").write_text("pdf", encoding="utf-8")

            summary = batch_convert.run_batch(
                input_dir,
                output_dir,
                engine="standard",
                md_engine=StubMarkItDown(),
            )

            self.assertEqual(summary.converted, 1)
            self.assertEqual(summary.failed, 1)
            collision = next(item for item in summary.results if item.status == "error")
            self.assertEqual(collision.output, "paper.md")
            self.assertIn("Output collision", collision.error)

    def test_rejects_output_directory_inside_input_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp) / "input"
            input_dir.mkdir()

            with self.assertRaises(batch_convert.BatchConfigurationError):
                batch_convert.run_batch(
                    input_dir,
                    input_dir / "output",
                    engine="standard",
                    md_engine=StubMarkItDown(),
                )

    def test_rejects_invalid_max_file_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp) / "input"
            input_dir.mkdir()

            with self.assertRaises(batch_convert.BatchConfigurationError):
                batch_convert.run_batch(
                    input_dir,
                    Path(tmp) / "output",
                    engine="standard",
                    md_engine=StubMarkItDown(),
                    max_file_size=0,
                )

    def test_oversized_file_records_error_without_writing_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "large.txt").write_bytes(b"123456")

            summary = batch_convert.run_batch(
                input_dir,
                output_dir,
                engine="standard",
                md_engine=StubMarkItDown(),
                max_file_size=5,
            )

            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.converted, 0)
            self.assertFalse((output_dir / "large.md").exists())
            self.assertIn("File too large", summary.results[0].error)


if __name__ == "__main__":
    unittest.main()
