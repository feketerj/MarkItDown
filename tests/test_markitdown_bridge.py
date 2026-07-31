import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import markitdown_bridge


class MarkItDownBridgeTests(unittest.TestCase):
    def test_refuses_high_ratio_archive_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "bomb.zip"
            with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("large.txt", "A" * (5 * 1024 * 1024))

            with self.assertRaisesRegex(RuntimeError, "compression ratio"):
                markitdown_bridge.convert_bounded(source, root / "out.md")
            self.assertFalse((root / "out.md").exists())

    def test_refuses_source_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real.txt"
            real.write_text("hello", encoding="utf-8")
            link = root / "link.txt"
            link.symlink_to(real)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                markitdown_bridge.convert_bounded(link, root / "out.md")

    def test_refuses_nested_archive_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inner = root / "inner.zip"
            with zipfile.ZipFile(
                inner,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr("large.txt", "A" * (5 * 1024 * 1024))
            outer = root / "outer.zip"
            with zipfile.ZipFile(
                outer,
                "w",
                compression=zipfile.ZIP_STORED,
            ) as archive:
                archive.write(inner, "inner.zip")

            with self.assertRaisesRegex(RuntimeError, "nested archive"):
                markitdown_bridge.convert_bounded(outer, root / "out.md")

    def test_output_writer_retries_short_writes(self):
        writes = []

        def short_write(_descriptor, payload):
            written = min(2, len(payload))
            writes.append(bytes(payload[:written]))
            return written

        with patch.object(markitdown_bridge.os, "write", side_effect=short_write):
            markitdown_bridge._write_all(99, b"abcdef")
        self.assertEqual(b"".join(writes), b"abcdef")

    def test_output_writer_refuses_zero_progress(self):
        with patch.object(markitdown_bridge.os, "write", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "no progress"):
                markitdown_bridge._write_all(99, b"abcdef")


if __name__ == "__main__":
    unittest.main()
