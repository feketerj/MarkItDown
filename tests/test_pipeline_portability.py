import base64
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pipeline_portability


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PipelinePortabilityTests(unittest.TestCase):
    def _claim(self, role, path, locator=None, run_id=None):
        data = path.read_bytes()
        return {
            "run_id": run_id or f"run-{role}-{path.name}",
            "role": role,
            "path": str(path),
            "historic_locator": locator or f"file://{path}",
            "expected_sha256": digest(data),
        }

    def _fixture(self, root):
        source = root / "source"
        source.mkdir()
        receipt = source / "artifact.receipt.json"
        admission = source / "candidate.admission.json"
        markdown = source / "artifact.md"
        layout = source / "artifact.deepdoc-ocr.json"
        raw = source / "artifact.manual.pdf.pending"
        unrelated = source / "not-selected.txt"
        receipt.write_bytes(b'{"schema_version":"opics.document-distillation.v1"}\n')
        admission.write_bytes(b'{"status":"admitted"}\n')
        markdown.write_bytes(b"# Manual\n")
        layout.write_bytes(b'{"pages":[]}\n')
        raw.write_bytes(b"%PDF raw bytes")
        unrelated.write_bytes(b"must never enter bundle")
        claims = [
            self._claim("receipt", receipt),
            self._claim("admission", admission),
            self._claim("markdown", markdown),
            self._claim("layout", layout),
            self._claim("raw", raw),
        ]
        return claims, unrelated

    def test_build_is_deterministic_content_addressed_and_selection_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claims, unrelated = self._fixture(root)
            first = pipeline_portability.build_bundle(claims, root / "bundles")
            first_bytes = Path(first.bundle_path).read_bytes()
            second = pipeline_portability.build_bundle(
                list(reversed(claims)), root / "bundles"
            )

            self.assertEqual(first, second)
            self.assertEqual(Path(second.bundle_path).read_bytes(), first_bytes)
            self.assertTrue(Path(first.bundle_path).name.startswith(first.bundle_id))
            envelope = json.loads(gzip.decompress(first_bytes))
            self.assertEqual(
                envelope["schema_version"], pipeline_portability.BUNDLE_SCHEMA
            )
            self.assertEqual(len(envelope["bundle"]["claims"]), 5)
            provenance = envelope["bundle"]["a7_code_provenance"]
            self.assertEqual(
                provenance["schema_version"],
                pipeline_portability.PROVENANCE_SCHEMA,
            )
            self.assertIn("pipeline_portability.py", provenance["owner_code_sha256"])
            embedded = {
                base64.b64decode(item["payload"]["data"])
                for item in envelope["bundle"]["claims"]
            }
            self.assertNotIn(unrelated.read_bytes(), embedded)

    def test_build_refuses_missing_symlink_hash_mismatch_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "manual.md"
            file_path.write_bytes(b"manual")
            good = self._claim("markdown", file_path)

            missing = dict(good, path=str(root / "missing.md"))
            with self.assertRaisesRegex(pipeline_portability.PortabilityError, "missing"):
                pipeline_portability.build_bundle([missing], root / "one")

            link = root / "link.md"
            link.symlink_to(file_path)
            linked = dict(good, path=str(link))
            with self.assertRaisesRegex(pipeline_portability.PortabilityError, "symlink"):
                pipeline_portability.build_bundle([linked], root / "two")

            mismatch = dict(good, expected_sha256="0" * 64)
            with self.assertRaisesRegex(pipeline_portability.PortabilityError, "hash mismatch"):
                pipeline_portability.build_bundle([mismatch], root / "three")

            traversal = dict(good, path=str(root / "unused" / ".." / "manual.md"))
            with self.assertRaisesRegex(pipeline_portability.PortabilityError, "traversal"):
                pipeline_portability.build_bundle([traversal], root / "four")

            locator_traversal = dict(good, historic_locator="file:///old/../manual.md")
            with self.assertRaisesRegex(pipeline_portability.PortabilityError, "traversal"):
                pipeline_portability.build_bundle([locator_traversal], root / "five")

            real_parent = root / "real-parent"
            real_parent.mkdir()
            nested = real_parent / "nested.md"
            nested.write_bytes(b"nested")
            alias_parent = root / "alias-parent"
            alias_parent.symlink_to(real_parent, target_is_directory=True)
            parent_linked = self._claim("markdown", alias_parent / "nested.md")
            with self.assertRaisesRegex(
                pipeline_portability.PortabilityError,
                "symlink component",
            ):
                pipeline_portability.build_bundle(
                    [parent_linked], root / "six"
                )

    def test_build_refuses_ambiguous_duplicate_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = root / "one"
            two = root / "two"
            one.mkdir()
            two.mkdir()
            first = one / "same.md"
            second = two / "same.md"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            claim = self._claim("markdown", first)
            with self.assertRaisesRegex(pipeline_portability.PortabilityError, "duplicate"):
                pipeline_portability.build_bundle([claim, claim], root / "bundles")

            colliding = self._claim("receipt", second)
            with self.assertRaisesRegex(pipeline_portability.PortabilityError, "destination"):
                pipeline_portability.build_bundle([claim, colliding], root / "bundles")

            historic_conflict = self._claim(
                "markdown",
                second,
                locator=claim["historic_locator"],
                run_id="another-run",
            )
            with self.assertRaisesRegex(
                pipeline_portability.PortabilityError, "historic locator"
            ):
                pipeline_portability.build_bundle(
                    [claim, historic_conflict], root / "bundles"
                )

    def test_same_historic_file_across_runs_is_explicit_not_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = root / "receipt.json"
            receipt.write_bytes(b"receipt")
            locator = "file:///historic/candidate/receipt.json"
            first = self._claim("receipt", receipt, locator, "run-001")
            second = self._claim("receipt", receipt, locator, "run-002")

            bundle = pipeline_portability.build_bundle(
                [first, second], root / "bundles"
            )
            verification = pipeline_portability.verify_bundle(bundle.bundle_path)

            self.assertEqual(len(verification.claim_ids), 2)
            result = pipeline_portability.restore_bundle(
                bundle.bundle_path,
                root / "artifacts",
                root / "intake",
            )
            self.assertEqual(len(result.restored_claim_ids), 2)
            self.assertEqual(len(set(result.restored_paths)), 1)

    def test_restore_preserves_artifact_names_quarantines_raw_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claims, unrelated = self._fixture(root)
            bundle = pipeline_portability.build_bundle(claims, root / "bundles")
            artifact_root = root / "restored-artifacts"
            intake_root = root / "restored-intake"

            first = pipeline_portability.restore_bundle(
                bundle.bundle_path, artifact_root, intake_root
            )
            manifest_bytes = Path(first.mapping_manifest_path).read_bytes()
            restored = {Path(path).name: Path(path) for path in first.restored_paths}
            for claim in claims:
                original = Path(claim["path"])
                self.assertEqual(restored[original.name].read_bytes(), original.read_bytes())
            self.assertEqual(
                restored[Path(claims[-1]["path"]).name].parent,
                (intake_root / pipeline_portability.QUARANTINE_DIR).resolve(),
            )
            self.assertFalse((artifact_root / unrelated.name).exists())

            manifest = json.loads(manifest_bytes)
            core = manifest["mapping"]
            self.assertEqual(manifest["mapping_id"], first.mapping_id)
            self.assertEqual(core["bundle_id"], bundle.bundle_id)
            self.assertEqual(core["bundle_sha256"], bundle.bundle_sha256)
            self.assertEqual(
                core["bundle_payload_sha256"], bundle.bundle_payload_sha256
            )
            self.assertEqual(len(core["mappings"]), 5)
            self.assertTrue(all(item["restored_path"] for item in core["mappings"]))
            self.assertEqual(
                pipeline_portability.load_mapping_manifest(
                    first.mapping_manifest_path
                )["mapping_id"],
                first.mapping_id,
            )

            second = pipeline_portability.restore_bundle(
                bundle.bundle_path, artifact_root, intake_root
            )
            self.assertEqual(first, second)
            self.assertEqual(Path(second.mapping_manifest_path).read_bytes(), manifest_bytes)

            restored["artifact.md"].write_bytes(b"changed after mapping")
            with self.assertRaisesRegex(
                pipeline_portability.PortabilityError,
                "mapped restored file hash",
            ):
                pipeline_portability.load_mapping_manifest(
                    second.mapping_manifest_path
                )

    def test_restore_subset_writes_only_requested_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claims, _ = self._fixture(root)
            bundle = pipeline_portability.build_bundle(claims, root / "bundles")
            envelope = json.loads(gzip.decompress(Path(bundle.bundle_path).read_bytes()))
            markdown = next(
                item
                for item in envelope["bundle"]["claims"]
                if item["role"] == "markdown"
            )

            result = pipeline_portability.restore_bundle(
                bundle.bundle_path,
                root / "artifacts",
                root / "intake",
                selected_claim_ids=[markdown["claim_id"]],
            )

            self.assertEqual(result.restored_claim_ids, [markdown["claim_id"]])
            self.assertEqual([Path(path).name for path in result.restored_paths], ["artifact.md"])
            self.assertFalse((root / "artifacts" / "artifact.receipt.json").exists())
            manifest = json.loads(Path(result.mapping_manifest_path).read_bytes())
            self.assertEqual(len(manifest["mapping"]["mappings"]), 1)

    def test_restore_refuses_conflicting_existing_bytes_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claims, _ = self._fixture(root)
            bundle = pipeline_portability.build_bundle(claims, root / "bundles")
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            (artifact_root / "artifact.md").write_bytes(b"conflict")

            with self.assertRaisesRegex(
                pipeline_portability.PortabilityError, "conflicting bytes"
            ):
                pipeline_portability.restore_bundle(
                    bundle.bundle_path, artifact_root, root / "intake"
                )
            self.assertEqual((artifact_root / "artifact.md").read_bytes(), b"conflict")
            self.assertFalse((artifact_root / "artifact.receipt.json").exists())

    def test_manifest_conflict_refuses_before_any_selected_file_is_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claims, _ = self._fixture(root)
            bundle = pipeline_portability.build_bundle(claims, root / "bundles")
            manifest = root / "mapping.json"
            manifest.write_bytes(b"conflicting manifest")
            artifact_root = root / "artifacts"
            intake_root = root / "intake"

            with self.assertRaisesRegex(
                pipeline_portability.PortabilityError,
                "conflicting bytes",
            ):
                pipeline_portability.restore_bundle(
                    bundle.bundle_path,
                    artifact_root,
                    intake_root,
                    mapping_manifest_path=manifest,
                )

            for claim in claims:
                selected = Path(claim["path"])
                target = (
                    intake_root / pipeline_portability.QUARANTINE_DIR / selected.name
                    if claim["role"] == "raw"
                    else artifact_root / selected.name
                )
                self.assertFalse(target.exists())

    def test_restore_refuses_payload_tamper_and_bundle_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claims, _ = self._fixture(root)
            bundle = pipeline_portability.build_bundle(claims, root / "bundles")
            original = Path(bundle.bundle_path)
            envelope = json.loads(gzip.decompress(original.read_bytes()))
            entry = envelope["bundle"]["claims"][0]
            entry["payload"]["data"] = base64.b64encode(b"tampered").decode("ascii")
            core = envelope["bundle"]
            envelope["bundle_id"] = digest(pipeline_portability._canonical_json(core))
            tampered = root / "tampered.gz"
            tampered.write_bytes(
                pipeline_portability._deterministic_gzip(
                    pipeline_portability._canonical_json(envelope)
                )
            )
            with self.assertRaisesRegex(pipeline_portability.PortabilityError, "payload"):
                pipeline_portability.restore_bundle(
                    tampered, root / "artifacts", root / "intake"
                )

            noncanonical = root / "noncanonical.gz"
            noncanonical.write_bytes(original.read_bytes() + b"\x00")
            with self.assertRaisesRegex(
                pipeline_portability.PortabilityError,
                "canonical deterministic",
            ):
                pipeline_portability.verify_bundle(noncanonical)

            link = root / "bundle-link.gz"
            link.symlink_to(original)
            with self.assertRaisesRegex(pipeline_portability.PortabilityError, "symlink"):
                pipeline_portability.restore_bundle(
                    link, root / "artifacts", root / "intake"
                )

    def test_malformed_external_bundle_and_mapping_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claims, _ = self._fixture(root)
            bundle = pipeline_portability.build_bundle(claims, root / "bundles")
            envelope = json.loads(gzip.decompress(Path(bundle.bundle_path).read_bytes()))
            envelope["bundle"]["claims"][0]["role"] = []
            envelope["bundle_id"] = digest(
                pipeline_portability._canonical_json(envelope["bundle"])
            )
            malformed = root / "malformed.gz"
            malformed.write_bytes(
                pipeline_portability._deterministic_gzip(
                    pipeline_portability._canonical_json(envelope)
                )
            )
            with self.assertRaises(pipeline_portability.PortabilityError):
                pipeline_portability.verify_bundle(malformed)
            self.assertEqual(
                pipeline_portability.main(["verify", str(malformed)]),
                2,
            )

            bad_provenance = json.loads(
                gzip.decompress(Path(bundle.bundle_path).read_bytes())
            )
            provenance = bad_provenance["bundle"]["a7_code_provenance"]
            provenance["owner_repository"] = None
            provenance["owner_worktree_state"] = "clean"
            provenance["owner_commit"] = None
            provenance["owner_head_commit"] = None
            provenance["owner_code_sha256"] = {"not-a7.py": "0" * 64}
            bad_provenance["bundle_id"] = digest(
                pipeline_portability._canonical_json(bad_provenance["bundle"])
            )
            bad_provenance_path = root / "bad-provenance.gz"
            bad_provenance_path.write_bytes(
                pipeline_portability._deterministic_gzip(
                    pipeline_portability._canonical_json(bad_provenance)
                )
            )
            with self.assertRaises(pipeline_portability.PortabilityError):
                pipeline_portability.verify_bundle(bad_provenance_path)

            restored = pipeline_portability.restore_bundle(
                bundle.bundle_path,
                root / "artifacts",
                root / "intake",
            )
            manifest_path = Path(restored.mapping_manifest_path)
            manifest = json.loads(manifest_path.read_bytes())
            manifest["mapping"]["mappings"][0]["restored_path"] = []
            manifest["mapping_id"] = digest(
                pipeline_portability._canonical_json(manifest["mapping"])
            )
            forged = root / "forged-mapping.json"
            forged.write_bytes(pipeline_portability._canonical_json(manifest) + b"\n")
            with self.assertRaises(pipeline_portability.PortabilityError):
                pipeline_portability.load_mapping_manifest(forged)

            outside_raw = json.loads(manifest_path.read_bytes())
            raw_mapping = next(
                item
                for item in outside_raw["mapping"]["mappings"]
                if item["role"] == "raw"
            )
            raw_mapping["restored_relative_path"] = Path(
                raw_mapping["restored_path"]
            ).name
            raw_mapping["restored_path"] = str(
                Path(outside_raw["mapping"]["intake_root"])
                / raw_mapping["restored_relative_path"]
            )
            outside_raw["mapping_id"] = digest(
                pipeline_portability._canonical_json(outside_raw["mapping"])
            )
            outside_raw_path = root / "outside-raw-mapping.json"
            outside_raw_path.write_bytes(
                pipeline_portability._canonical_json(outside_raw) + b"\n"
            )
            with self.assertRaisesRegex(
                pipeline_portability.PortabilityError,
                "outside the distillation quarantine",
            ):
                pipeline_portability.load_mapping_manifest(outside_raw_path)

            traversal_mapping = json.loads(manifest_path.read_bytes())
            artifact_root = Path(traversal_mapping["mapping"]["artifact_root"])
            shim = artifact_root.parent / "shim"
            shim.mkdir()
            traversal_mapping["mapping"]["artifact_root"] = str(
                shim / ".." / artifact_root.name
            )
            traversal_mapping["mapping_id"] = digest(
                pipeline_portability._canonical_json(
                    traversal_mapping["mapping"]
                )
            )
            traversal_mapping_path = root / "traversal-mapping.json"
            traversal_mapping_path.write_bytes(
                pipeline_portability._canonical_json(traversal_mapping) + b"\n"
            )
            with self.assertRaisesRegex(
                pipeline_portability.PortabilityError,
                "canonical paths",
            ):
                pipeline_portability.load_mapping_manifest(
                    traversal_mapping_path
                )

    def test_restore_refuses_symlink_destination_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claims, _ = self._fixture(root)
            bundle = pipeline_portability.build_bundle(claims, root / "bundles")
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            outside = root / "outside.md"
            outside.write_bytes(b"outside")
            (artifact_root / "artifact.md").symlink_to(outside)

            with self.assertRaisesRegex(
                pipeline_portability.PortabilityError,
                "symlink",
            ):
                pipeline_portability.restore_bundle(
                    bundle.bundle_path,
                    artifact_root,
                    root / "intake",
                )
            self.assertEqual(outside.read_bytes(), b"outside")
            self.assertFalse((artifact_root / "artifact.receipt.json").exists())

    def test_cli_build_and_restore_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claims, _ = self._fixture(root)
            selection = root / "selection.json"
            selection.write_text(
                json.dumps(
                    {
                        "schema_version": pipeline_portability.SELECTION_SCHEMA,
                        "claims": claims,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                pipeline_portability.main(
                    [
                        "build",
                        str(selection),
                        str(root / "custody.json.gz"),
                    ]
                ),
                0,
            )
            self.assertEqual(
                pipeline_portability.main(
                    ["verify", str(root / "custody.json.gz")]
                ),
                0,
            )
            bundle = root / "custody.json.gz"
            self.assertEqual(
                pipeline_portability.main(
                    [
                        "restore",
                        str(bundle),
                        str(root / "artifacts"),
                        str(root / "intake"),
                        "--manifest",
                        str(root / "mapping.json"),
                    ]
                ),
                0,
            )
            self.assertEqual(
                pipeline_portability.main(
                    ["mapping", str(root / "mapping.json")]
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
