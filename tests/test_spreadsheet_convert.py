import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from spreadsheet_convert import (
    SpreadsheetConversionOptions,
    SpreadsheetLimitError,
    convert_spreadsheet_to_path,
    is_spreadsheet_path,
)


class SpreadsheetConvertTests(unittest.TestCase):
    def test_csv_streams_full_output_and_bounds_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "large.csv"
            target = root / "large.md"
            source.write_text(
                'Name,Note\nAlice,"Hello|world"\nBob,"Line 1\nLine 2"\nCarol,Done\n',
                encoding="utf-8",
            )

            result = convert_spreadsheet_to_path(
                source,
                target,
                SpreadsheetConversionOptions(preview_rows_per_sheet=1),
            )

            markdown = target.read_text(encoding="utf-8")
            self.assertTrue(result.preview_truncated)
            self.assertEqual(result.markdown_length, len(markdown))
            self.assertIn("## large", markdown)
            self.assertIn(r"| Alice | Hello\|world |", markdown)
            self.assertIn("| Bob | Line 1<br>Line 2 |", markdown)
            self.assertIn("| Carol | Done |", markdown)
            self.assertIn("Preview truncated after 1 data rows", result.preview)
            self.assertNotIn("Carol", result.preview)

    def test_xlsx_converts_multiple_sheets_without_pandas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "book.xlsx"
            target = root / "book.md"

            workbook = Workbook()
            first = workbook.active
            first.title = "Summary"
            first.append(["Account", "Amount"])
            first.append(["Sales", 1250])
            second = workbook.create_sheet("Notes")
            second.append(["Owner", "Comment"])
            second.append(["Ops", "ready"])
            workbook.save(source)

            result = convert_spreadsheet_to_path(source, target)
            markdown = target.read_text(encoding="utf-8")

            self.assertEqual(result.sheets, 2)
            self.assertIn("## Summary", markdown)
            self.assertIn("| Account | Amount |", markdown)
            self.assertIn("| Sales | 1250 |", markdown)
            self.assertIn("## Notes", markdown)
            self.assertIn("| Ops | ready |", markdown)

    def test_csv_handles_many_rows_with_bounded_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "many.csv"
            target = root / "many.md"
            rows = ["Name,Value"] + [f"row-{index},{index}" for index in range(2000)]
            source.write_text("\n".join(rows) + "\n", encoding="utf-8")

            result = convert_spreadsheet_to_path(
                source,
                target,
                SpreadsheetConversionOptions(preview_rows_per_sheet=3),
            )

            markdown = target.read_text(encoding="utf-8")
            self.assertEqual(result.rows, 2001)
            self.assertTrue(result.preview_truncated)
            self.assertIn("| row-1999 | 1999 |", markdown)
            self.assertIn("| row-2 | 2 |", result.preview)
            self.assertNotIn("row-1999", result.preview)

    def test_column_limit_fails_before_writing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "wide.csv"
            target = root / "wide.md"
            source.write_text("a,b,c\n1,2,3\n", encoding="utf-8")

            with self.assertRaises(SpreadsheetLimitError):
                convert_spreadsheet_to_path(
                    source,
                    target,
                    SpreadsheetConversionOptions(max_columns=2),
                )

            self.assertFalse(target.exists())

    def test_output_limit_fails_before_writing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "rows.csv"
            target = root / "rows.md"
            source.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")

            with self.assertRaises(SpreadsheetLimitError):
                convert_spreadsheet_to_path(
                    source,
                    target,
                    SpreadsheetConversionOptions(max_output_chars=10),
                )

            self.assertFalse(target.exists())

    def test_spreadsheet_detection_is_extension_based(self):
        self.assertTrue(is_spreadsheet_path("report.xlsx"))
        self.assertTrue(is_spreadsheet_path("report.xls"))
        self.assertTrue(is_spreadsheet_path("report.csv"))
        self.assertFalse(is_spreadsheet_path("report.pdf"))


if __name__ == "__main__":
    unittest.main()
