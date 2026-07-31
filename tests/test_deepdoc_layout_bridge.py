import tempfile
import unittest
from pathlib import Path

import deepdoc_layout_bridge


class FakePage:
    def __init__(self, text):
        self.text = text

    def get_text(self, mode):
        return self.text


class DeepDocLayoutBridgeTests(unittest.TestCase):
    def test_stratified_selector_does_not_starve_specification_page(self):
        pages = [FakePage("") for _ in range(8)]
        pages.extend(FakePage("ordinary content") for _ in range(11))
        pages.append(
            FakePage(
                "specifications dimensions capacity spindle accuracy tolerance"
            )
        )

        selected = deepdoc_layout_bridge._select_pages(pages, max_pages=8)

        self.assertEqual(len(selected), 8)
        self.assertIn((19, "specification_keyword"), selected)
        self.assertTrue(any(reason == "scan_sample" for _, reason in selected))

    def test_bridge_refuses_to_overwrite_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "manual.pdf"
            source.write_bytes(b"not a PDF")

            with self.assertRaisesRegex(RuntimeError, "must not overwrite"):
                deepdoc_layout_bridge.extract_layout(
                    source,
                    source,
                    deepdoc_root=tmp,
                )


if __name__ == "__main__":
    unittest.main()
