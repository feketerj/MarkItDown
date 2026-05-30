import unittest

from markdown_cleanup import normalize_markdown_for_source, normalize_pptx_slide_markers


class MarkdownCleanupTests(unittest.TestCase):
    def test_pptx_slide_markers_become_markdown_sections(self):
        markdown = "\n\n<!-- Slide number: 1 -->\n# Intro\n\n<!-- Slide number: 2 -->\nBody"

        cleaned = normalize_pptx_slide_markers(markdown)

        self.assertTrue(cleaned.startswith("### Slide Number: 1\n\n# Intro"))
        self.assertIn("\n---\n\n### Slide Number: 2\n\nBody", cleaned)
        self.assertNotIn("<!-- Slide number", cleaned)
        self.assertNotIn("---\n\n### Slide Number: 1", cleaned)
        self.assertFalse(cleaned.startswith("---"))

    def test_pptx_slide_marker_with_trailing_text_is_preserved(self):
        cleaned = normalize_pptx_slide_markers("<!-- Slide number: 7 --> Agenda")

        self.assertEqual(cleaned, "### Slide Number: 7\n\nAgenda")

    def test_non_pptx_source_is_unchanged(self):
        markdown = "<!-- Slide number: 1 -->\nThis is real input."

        self.assertEqual(normalize_markdown_for_source(markdown, "notes.md"), markdown)

    def test_large_deck_cleanup_is_linear_and_counts_boundaries(self):
        markdown = "\n".join(
            f"<!-- Slide number: {index} -->\nSlide {index} content"
            for index in range(1, 1001)
        )

        cleaned = normalize_markdown_for_source(markdown, "big-deck.pptx")

        self.assertEqual(cleaned.count("### Slide Number:"), 1000)
        self.assertEqual(cleaned.count("\n---\n"), 999)
        self.assertNotIn("<!-- Slide number", cleaned)
        self.assertTrue(cleaned.startswith("### Slide Number: 1"))
        self.assertFalse(cleaned.startswith("---"))
        self.assertIn("### Slide Number: 1000\n\nSlide 1000 content", cleaned)


if __name__ == "__main__":
    unittest.main()
