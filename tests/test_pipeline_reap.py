import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pipeline_distill
import pipeline_reap


class StubMarkItDown:
    def convert(self, path):
        return SimpleNamespace(text_content="manual text")


class PipelineReapTests(unittest.TestCase):
    def _distill(self, root, *, authorize=True):
        intake = root / "intake"
        output = root / "artifacts"
        intake.mkdir()
        source = intake / "manual.txt"
        source.write_text("manual bytes", encoding="utf-8")
        result = pipeline_distill.distill_file(
            source,
            output,
            source_uri="https://oem.example/manual.txt",
            parser="markitdown",
            owned_root=intake,
            markitdown_engine=StubMarkItDown(),
        )
        if authorize:
            field_receipt = output / "machine-test.field-receipt.json"
            field_receipt.write_text(
                '{"status":"SCHEMA_EXTRACTION_COMPLETE"}\n',
                encoding="utf-8",
            )
            pipeline_reap.write_cleanup_authorization(
                result.receipt_path,
                pipeline="machine_specs",
                subject_id="machine:test",
                terminal_disposition="SCHEMA_EXTRACTION_COMPLETE",
                evidence_paths={"field_receipt": field_receipt},
            )
        return intake, output, result

    def test_verified_quarantine_is_deleted_with_append_only_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake, output, result = self._distill(root)
            receipt_path = Path(result.receipt_path)
            receipt_hash = hashlib.sha256(receipt_path.read_bytes()).hexdigest()

            results = pipeline_reap.reap_quarantine(
                intake,
                output,
                min_age_seconds=0,
            )

            deleted = next(item for item in results if item.receipt == receipt_path.name)
            self.assertEqual(deleted.status, "DELETED")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertFalse((intake / receipt["cleanup"]["quarantine_name"]).exists())
            self.assertEqual(
                hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                receipt_hash,
            )
            event_path = (
                output
                / "cleanup-events"
                / f"{receipt_path.name}.cleanup.jsonl"
            )
            events = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([event["status"] for event in events], ["AUTHORIZED", "DELETED"])
            self.assertTrue(all(event["receipt_sha256"] == receipt_hash for event in events))

            repeated = pipeline_reap.reap_quarantine(
                intake,
                output,
                min_age_seconds=0,
            )
            already = next(
                item for item in repeated if item.receipt == receipt_path.name
            )
            self.assertEqual(already.status, "ALREADY_DELETED")

    def test_hash_mismatch_is_refused_and_raw_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake, output, result = self._distill(root)
            receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
            quarantined = intake / receipt["cleanup"]["quarantine_name"]
            quarantined.write_text("tampered", encoding="utf-8")

            results = pipeline_reap.reap_quarantine(
                intake,
                output,
                min_age_seconds=0,
            )

            refused = next(item for item in results if item.receipt == Path(result.receipt_path).name)
            self.assertEqual(refused.status, "REFUSED")
            self.assertTrue(quarantined.exists())
            self.assertIn("do not match", refused.detail)

    def test_dry_run_never_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake, output, result = self._distill(root)
            receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
            quarantined = intake / receipt["cleanup"]["quarantine_name"]

            results = pipeline_reap.reap_quarantine(
                intake,
                output,
                min_age_seconds=0,
                dry_run=True,
            )

            dry_run = next(item for item in results if item.receipt == Path(result.receipt_path).name)
            self.assertEqual(dry_run.status, "DRY_RUN")
            self.assertTrue(quarantined.exists())

    def test_age_uses_quarantine_receipt_not_source_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake, output, result = self._distill(root)
            receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
            quarantined = intake / receipt["cleanup"]["quarantine_name"]
            os.utime(quarantined, (1, 1))

            results = pipeline_reap.reap_quarantine(
                intake,
                output,
                min_age_seconds=3600,
            )

            deferred = next(
                item for item in results if item.receipt == Path(result.receipt_path).name
            )
            self.assertEqual(deferred.status, "DEFERRED")
            self.assertTrue(quarantined.exists())

    def test_unexplained_absence_without_deleted_event_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake, output, result = self._distill(root)
            receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
            quarantined = intake / receipt["cleanup"]["quarantine_name"]
            quarantined.unlink()

            results = pipeline_reap.reap_quarantine(
                intake,
                output,
                min_age_seconds=0,
            )

            refused = next(
                item for item in results if item.receipt == Path(result.receipt_path).name
            )
            self.assertEqual(refused.status, "REFUSED")
            self.assertIn("without a DELETED event", refused.detail)

    def test_distillation_receipt_alone_never_authorizes_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake, output, result = self._distill(root, authorize=False)
            receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
            quarantined = intake / receipt["cleanup"]["quarantine_name"]

            results = pipeline_reap.reap_quarantine(
                intake,
                output,
                min_age_seconds=0,
            )

            refused = next(
                item for item in results if item.receipt == Path(result.receipt_path).name
            )
            self.assertEqual(refused.status, "REFUSED")
            self.assertIn("authorization is missing", refused.detail)
            self.assertTrue(quarantined.exists())

    def test_cleanup_authorization_is_idempotent_and_symlink_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, output, result = self._distill(root, authorize=False)
            kwargs = {
                "pipeline": "machine_specs",
                "subject_id": "machine:test",
                "terminal_disposition": "SCHEMA_EXTRACTION_COMPLETE",
            }
            field_receipt = output / "machine-test.field-receipt.json"
            field_receipt.write_text(
                '{"status":"SCHEMA_EXTRACTION_COMPLETE"}\n',
                encoding="utf-8",
            )
            kwargs["evidence_paths"] = {"field_receipt": field_receipt}
            first = pipeline_reap.write_cleanup_authorization(
                result.receipt_path,
                **kwargs,
            )
            first_bytes = first.read_bytes()
            second = pipeline_reap.write_cleanup_authorization(
                result.receipt_path,
                **kwargs,
            )
            self.assertEqual(first, second)
            self.assertEqual(first.read_bytes(), first_bytes)
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)

            symlink = output / "receipt-link.json"
            symlink.symlink_to(Path(result.receipt_path))
            with self.assertRaisesRegex(ValueError, "symlink"):
                pipeline_reap.write_cleanup_authorization(symlink, **kwargs)

    def test_missing_or_changed_downstream_evidence_refuses_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake, output, result = self._distill(root, authorize=False)
            evidence = output / "field-receipt.json"
            evidence.write_text('{"status":"complete"}\n', encoding="utf-8")
            pipeline_reap.write_cleanup_authorization(
                result.receipt_path,
                pipeline="machine_specs",
                subject_id="machine:test",
                terminal_disposition="SCHEMA_EXTRACTION_COMPLETE",
                evidence_paths={"field_receipt": evidence},
            )
            evidence.write_text('{"status":"tampered"}\n', encoding="utf-8")

            results = pipeline_reap.reap_quarantine(
                intake,
                output,
                min_age_seconds=0,
            )
            refused = next(
                item for item in results
                if item.receipt == Path(result.receipt_path).name
            )
            self.assertEqual(refused.status, "REFUSED")
            self.assertIn("evidence artifact changed", refused.detail)

            missing = output / "missing-field-receipt.json"
            with self.assertRaisesRegex(ValueError, "missing"):
                pipeline_reap.write_cleanup_authorization(
                    result.receipt_path,
                    pipeline="machine_specs",
                    subject_id="machine:other",
                    terminal_disposition="SCHEMA_EXTRACTION_COMPLETE",
                    evidence_paths={"field_receipt": missing},
                )


if __name__ == "__main__":
    unittest.main()
