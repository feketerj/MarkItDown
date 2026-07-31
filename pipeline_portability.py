#!/usr/bin/env python3
"""Portable, content-addressed custody bundles for selected OPICS documents.

The caller names every file admitted to a bundle.  This module never walks an
artifact or intake directory and never infers additional files from a receipt.
Historic locators are provenance only; restore destinations are derived from
the selected file basename and its governed role.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import gzip
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


SELECTION_SCHEMA = "opics.document-custody-selection.v1"
BUNDLE_SCHEMA = "opics.document-custody-bundle.v1"
BUNDLE_CORE_SCHEMA = "opics.document-custody-bundle-core.v1"
CLAIM_SCHEMA = "opics.document-custody-claim.v1"
PAYLOAD_SCHEMA = "opics.document-custody-payload.v1"
PROVENANCE_SCHEMA = "opics.a7-code-provenance.v1"
MAPPING_SCHEMA = "opics.document-custody-mapping.v1"
MAPPING_CORE_SCHEMA = "opics.document-custody-mapping-core.v1"
BUNDLE_SUFFIX = ".opics-custody.json.gz"
VALID_ROLES = {"admission", "receipt", "markdown", "layout", "raw"}
QUARANTINE_DIR = ".opics-distill-quarantine"
MAPPING_DIR = ".opics-portability-mappings"
IMPLEMENTATION_VERSION = 1
MAX_CLAIM_BYTES = 512 * 1024 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024 * 1024
OWNER_CODE_FILES = ("pipeline_portability.py",)
OWNER_REPOSITORY = "https://github.com/feketerj/MarkItDown"


class PortabilityError(ValueError):
    """Raised when custody cannot be bundled or restored without ambiguity."""


@dataclass(frozen=True)
class FileClaim:
    run_id: str
    role: str
    path: str
    historic_locator: str
    expected_sha256: str


@dataclass(frozen=True)
class BundleResult:
    bundle_path: str
    bundle_id: str
    bundle_payload_sha256: str
    bundle_sha256: str
    selection_sha256: str
    claim_count: int


@dataclass(frozen=True)
class RestoreResult:
    mapping_manifest_path: str
    mapping_id: str
    bundle_id: str
    restored_claim_ids: list[str]
    restored_paths: list[str]


@dataclass(frozen=True)
class BundleVerification:
    bundle_id: str
    bundle_payload_sha256: str
    bundle_sha256: str
    selection_sha256: str
    claim_ids: list[str]


def load_selection(path: str | Path) -> list[FileClaim]:
    """Load the strict JSON selection document used by the CLI."""

    supplied = Path(path).expanduser()
    raw = _read_regular_file(supplied, MAX_BUNDLE_BYTES, "selection")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortabilityError("selection must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "claims"}:
        raise PortabilityError("selection must contain only schema_version and claims")
    if payload["schema_version"] != SELECTION_SCHEMA:
        raise PortabilityError(f"selection schema must be {SELECTION_SCHEMA}")
    if not isinstance(payload["claims"], list):
        raise PortabilityError("selection claims must be a JSON array")
    return [_coerce_claim(item) for item in payload["claims"]]


def build_bundle(
    claims: Sequence[FileClaim | Mapping[str, str]],
    output_dir: str | Path | None = None,
    *,
    bundle_path: str | Path | None = None,
) -> BundleResult:
    """Build one deterministic gzip/JSON bundle from explicit file claims.

    With ``output_dir``, the returned filename is the SHA-256 of the canonical
    bundle core. ``bundle_path`` lets an orchestrator name the immutable file.
    Either form emits identical bytes for identical claims and code provenance.
    """

    if (output_dir is None) == (bundle_path is None):
        raise PortabilityError("provide exactly one of output_dir or bundle_path")
    normalized = [_coerce_claim(claim) for claim in claims]
    if not normalized:
        raise PortabilityError("at least one explicit file claim is required")

    entries = []
    seen_paths: dict[str, tuple] = {}
    seen_locators: dict[str, tuple] = {}
    seen_destinations: dict[tuple[str, str], tuple] = {}
    seen_claim_ids: set[str] = set()
    total_size = 0
    for claim in normalized:
        source, payload = _read_claim(claim)
        total_size += len(payload)
        if total_size > MAX_BUNDLE_BYTES:
            raise PortabilityError("selected payload exceeds the bundle byte limit")
        source_key = os.path.normcase(str(source))
        destination_key = (
            "intake" if claim.role == "raw" else "artifact",
            source.name,
        )
        equivalence = (
            claim.role,
            claim.historic_locator,
            claim.expected_sha256,
            destination_key,
        )
        _admit_duplicate_equivalence(
            seen_paths,
            source_key,
            equivalence,
            "same file",
        )
        _admit_duplicate_equivalence(
            seen_locators,
            claim.historic_locator,
            (claim.role, claim.expected_sha256, destination_key),
            "historic locator",
        )
        _admit_duplicate_equivalence(
            seen_destinations,
            destination_key,
            (claim.role, claim.historic_locator, claim.expected_sha256),
            "restore destination",
        )

        identity_material = {
            "run_id": claim.run_id,
            "role": claim.role,
            "historic_locator": claim.historic_locator,
            "expected_sha256": claim.expected_sha256,
        }
        claim_id = _sha256(_canonical_json(identity_material))
        claim_metadata = {
            "schema_version": CLAIM_SCHEMA,
            **identity_material,
            "original_name": source.name,
        }
        if claim_id in seen_claim_ids:
            raise PortabilityError("ambiguous duplicate claim identity")
        entry = {
            **claim_metadata,
            "claim_id": claim_id,
            "payload": {
                "schema_version": PAYLOAD_SCHEMA,
                "version": IMPLEMENTATION_VERSION,
                "encoding": "base64",
                "size_bytes": len(payload),
                "sha256": claim.expected_sha256,
                "data": base64.b64encode(payload).decode("ascii"),
            },
        }
        entries.append(entry)
        seen_claim_ids.add(claim_id)

    entries.sort(key=lambda item: item["claim_id"])
    selection_sha256 = _selection_sha256(entries)
    core = {
        "schema_version": BUNDLE_CORE_SCHEMA,
        "version": IMPLEMENTATION_VERSION,
        "selection_sha256": selection_sha256,
        "a7_code_provenance": _a7_code_provenance(),
        "claims": entries,
    }
    bundle_id = _sha256(_canonical_json(core))
    envelope = {
        "schema_version": BUNDLE_SCHEMA,
        "bundle_id": bundle_id,
        "bundle": core,
    }
    encoded = _canonical_json(envelope)
    bundle_payload_sha256 = _sha256(encoded)
    compressed = _deterministic_gzip(encoded)
    bundle_sha256 = _sha256(compressed)

    if bundle_path is None:
        destination_root = _prepare_root(output_dir, "output_dir")
        target_bundle = destination_root / f"{bundle_id}{BUNDLE_SUFFIX}"
    else:
        supplied_target = Path(bundle_path).expanduser()
        if supplied_target.is_symlink():
            raise PortabilityError("bundle_path must not be a symlink")
        if not supplied_target.name or supplied_target.name in {".", ".."}:
            raise PortabilityError("bundle_path must name a file")
        destination_root = _prepare_root(supplied_target.parent, "bundle parent")
        target_bundle = destination_root / supplied_target.name
    _publish_immutable(target_bundle, compressed, bundle_sha256)
    return BundleResult(
        bundle_path=str(target_bundle),
        bundle_id=bundle_id,
        bundle_payload_sha256=bundle_payload_sha256,
        bundle_sha256=bundle_sha256,
        selection_sha256=selection_sha256,
        claim_count=len(entries),
    )


def restore_bundle(
    bundle_path: str | Path,
    artifact_root: str | Path,
    intake_root: str | Path,
    *,
    selected_claim_ids: Iterable[str] | None = None,
    mapping_manifest_path: str | Path | None = None,
) -> RestoreResult:
    """Restore selected bundle claims beneath caller-owned custody roots.

    Raw claims land only in ``intake_root/.opics-distill-quarantine``.  All
    other roles retain their original basename directly under ``artifact_root``
    so a restored distillation receipt can still resolve its selected outputs.
    Existing identical bytes make the operation idempotent; different bytes at
    any destination fail closed before this call writes another artifact.
    """

    compressed, envelope, entries = _load_verified_bundle(bundle_path)
    bundle_sha256 = _sha256(compressed)
    bundle_payload_sha256 = _sha256(_canonical_json(envelope))
    bundle_id = envelope["bundle_id"]

    by_id = {entry["claim_id"]: entry for entry in entries}
    if selected_claim_ids is None:
        selected_ids = sorted(by_id)
    else:
        requested = list(selected_claim_ids)
        if not requested:
            raise PortabilityError("selected_claim_ids must not be empty")
        if len(requested) != len(set(requested)):
            raise PortabilityError("selected_claim_ids contains duplicates")
        invalid = [claim_id for claim_id in requested if not _is_sha256(claim_id)]
        if invalid:
            raise PortabilityError("selected_claim_ids contains an invalid identity")
        unknown = sorted(set(requested) - set(by_id))
        if unknown:
            raise PortabilityError(f"bundle does not contain claim {unknown[0]}")
        selected_ids = sorted(requested)

    artifact = _prepare_root(artifact_root, "artifact_root")
    intake = _prepare_root(intake_root, "intake_root")
    if artifact == intake or _is_relative_to(artifact, intake) or _is_relative_to(intake, artifact):
        raise PortabilityError("artifact_root and intake_root must be disjoint")
    quarantine = _prepare_child_directory(intake, QUARANTINE_DIR, private=True)
    mappings = _prepare_child_directory(artifact, MAPPING_DIR, private=True)

    planned = []
    for claim_id in selected_ids:
        entry = by_id[claim_id]
        payload = _payload_bytes(entry)
        target_root = quarantine if entry["role"] == "raw" else artifact
        target = target_root / entry["original_name"]
        planned.append((entry, payload, target))

    lock_path = mappings / f"{bundle_id}.restore.lock"
    lock_fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        mapping_entries = []
        for entry, _, target in planned:
            root_name = "intake_root" if entry["role"] == "raw" else "artifact_root"
            root_path = intake if entry["role"] == "raw" else artifact
            mapping_entries.append(
                {
                    "claim_id": entry["claim_id"],
                    "run_id": entry["run_id"],
                    "role": entry["role"],
                    "historic_locator": entry["historic_locator"],
                    "sha256": entry["expected_sha256"],
                    "restored_path": str(target),
                    "restored_root": root_name,
                    "restored_relative_path": str(target.relative_to(root_path)),
                }
            )
        mapping_entries.sort(key=lambda item: item["claim_id"])
        restored_selection_sha256 = _sha256(
            _canonical_json([item["claim_id"] for item in mapping_entries])
        )
        mapping_core = {
            "schema_version": MAPPING_CORE_SCHEMA,
            "version": IMPLEMENTATION_VERSION,
            "bundle_id": bundle_id,
            "bundle_payload_sha256": bundle_payload_sha256,
            "bundle_sha256": bundle_sha256,
            "bundle_selection_sha256": envelope["bundle"]["selection_sha256"],
            "restored_selection_sha256": restored_selection_sha256,
            "artifact_root": str(artifact),
            "intake_root": str(intake),
            "mappings": mapping_entries,
        }
        mapping_id = _sha256(_canonical_json(mapping_core))
        manifest = {
            "schema_version": MAPPING_SCHEMA,
            "mapping_id": mapping_id,
            "mapping": mapping_core,
        }
        if mapping_manifest_path is None:
            manifest_path = mappings / f"{mapping_id}.mapping.json"
        else:
            supplied_manifest = Path(mapping_manifest_path).expanduser()
            manifest_parent = _prepare_root(
                supplied_manifest.parent,
                "mapping manifest parent",
            )
            manifest_path = manifest_parent / supplied_manifest.name
        manifest_bytes = _canonical_json(manifest) + b"\n"
        manifest_sha256 = _sha256(manifest_bytes)
        if any(manifest_path == target for _, _, target in planned):
            raise PortabilityError("mapping manifest path conflicts with a restore target")
        for entry, _, target in planned:
            _preflight_target(target, entry["expected_sha256"])
        _preflight_target(manifest_path, manifest_sha256)
        for entry, payload, target in planned:
            _publish_immutable(target, payload, entry["expected_sha256"])
        _publish_immutable(manifest_path, manifest_bytes, manifest_sha256)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    return RestoreResult(
        mapping_manifest_path=str(manifest_path),
        mapping_id=mapping_id,
        bundle_id=bundle_id,
        restored_claim_ids=selected_ids,
        restored_paths=[str(target) for _, _, target in planned],
    )


def verify_bundle(path: str | Path) -> BundleVerification:
    """Validate a bundle without restoring it and return its bound identities."""

    compressed, envelope, entries = _load_verified_bundle(path)
    return BundleVerification(
        bundle_id=envelope["bundle_id"],
        bundle_payload_sha256=_sha256(_canonical_json(envelope)),
        bundle_sha256=_sha256(compressed),
        selection_sha256=envelope["bundle"]["selection_sha256"],
        claim_ids=[entry["claim_id"] for entry in entries],
    )


def load_mapping_manifest(path: str | Path) -> dict:
    """Load and hash-verify a restore mapping manifest."""

    raw = _read_regular_file(Path(path).expanduser(), MAX_BUNDLE_BYTES, "mapping manifest")
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortabilityError("mapping manifest must be valid UTF-8 JSON") from exc
    if raw not in {_canonical_json(manifest), _canonical_json(manifest) + b"\n"}:
        raise PortabilityError("mapping manifest JSON is not canonical")
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "mapping_id",
        "mapping",
    }:
        raise PortabilityError("mapping manifest envelope shape is invalid")
    if manifest["schema_version"] != MAPPING_SCHEMA:
        raise PortabilityError("mapping manifest schema is unsupported")
    core = manifest["mapping"]
    required = {
        "schema_version",
        "version",
        "bundle_id",
        "bundle_payload_sha256",
        "bundle_sha256",
        "bundle_selection_sha256",
        "restored_selection_sha256",
        "artifact_root",
        "intake_root",
        "mappings",
    }
    if not isinstance(core, dict) or set(core) != required:
        raise PortabilityError("mapping manifest core shape is invalid")
    if (
        core["schema_version"] != MAPPING_CORE_SCHEMA
        or type(core["version"]) is not int
        or core["version"] != IMPLEMENTATION_VERSION
    ):
        raise PortabilityError("mapping manifest core version is unsupported")
    if any(
        not isinstance(core[key], str) or not Path(core[key]).is_absolute()
        for key in ("artifact_root", "intake_root")
    ):
        raise PortabilityError("mapping manifest roots must be absolute paths")
    declared_roots = {
        key: Path(core[key])
        for key in ("artifact_root", "intake_root")
    }
    if any(
        ".." in root.parts or root != root.resolve()
        for root in declared_roots.values()
    ):
        raise PortabilityError("mapping manifest roots must be canonical paths")
    artifact_root = declared_roots["artifact_root"]
    intake_root = declared_roots["intake_root"]
    if (
        artifact_root == intake_root
        or _is_relative_to(artifact_root, intake_root)
        or _is_relative_to(intake_root, artifact_root)
    ):
        raise PortabilityError("mapping manifest roots must be disjoint")
    for key in (
        "bundle_id",
        "bundle_payload_sha256",
        "bundle_sha256",
        "bundle_selection_sha256",
        "restored_selection_sha256",
    ):
        if not _is_sha256(core[key]):
            raise PortabilityError(f"mapping manifest {key} is invalid")
    mappings = core["mappings"]
    if not isinstance(mappings, list) or not mappings:
        raise PortabilityError("mapping manifest mappings are missing")
    expected_entry_keys = {
        "claim_id",
        "run_id",
        "role",
        "historic_locator",
        "sha256",
        "restored_path",
        "restored_root",
        "restored_relative_path",
    }
    claim_ids = []
    for item in mappings:
        if not isinstance(item, dict) or set(item) != expected_entry_keys:
            raise PortabilityError("mapping manifest entry shape is invalid")
        if not _is_sha256(item["claim_id"]) or not _is_sha256(item["sha256"]):
            raise PortabilityError("mapping manifest entry hash is invalid")
        if (
            not isinstance(item["role"], str)
            or item["role"] not in VALID_ROLES
            or not isinstance(item["restored_root"], str)
            or item["restored_root"] not in {"artifact_root", "intake_root"}
        ):
            raise PortabilityError("mapping manifest entry role or root is invalid")
        if not isinstance(item["run_id"], str) or not item["run_id"]:
            raise PortabilityError("mapping manifest run identity is invalid")
        if not isinstance(item["historic_locator"], str) or not item["historic_locator"]:
            raise PortabilityError("mapping manifest historic locator is invalid")
        _validate_no_traversal(item["historic_locator"], "mapping historic locator")
        identity_material = {
            "run_id": item["run_id"],
            "role": item["role"],
            "historic_locator": item["historic_locator"],
            "expected_sha256": item["sha256"],
        }
        if item["claim_id"] != _sha256(_canonical_json(identity_material)):
            raise PortabilityError("mapping claim identity does not match its metadata")
        expected_root = "intake_root" if item["role"] == "raw" else "artifact_root"
        if item["restored_root"] != expected_root:
            raise PortabilityError("mapping role does not match its restored root")
        if not isinstance(item["restored_path"], str) or not isinstance(
            item["restored_relative_path"],
            str,
        ):
            raise PortabilityError("mapping restored path fields are invalid")
        restored = Path(item["restored_path"])
        if (
            not restored.is_absolute()
            or ".." in restored.parts
            or restored != restored.resolve()
        ):
            raise PortabilityError("mapping restored_path must be absolute")
        relative = Path(item["restored_relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise PortabilityError("mapping restored_relative_path is unsafe")
        if item["role"] == "raw":
            if len(relative.parts) != 2 or relative.parts[0] != QUARANTINE_DIR:
                raise PortabilityError("raw mapping is outside the distillation quarantine")
        elif len(relative.parts) != 1:
            raise PortabilityError("artifact mapping is outside the artifact root surface")
        root_key = item["restored_root"]
        declared_root = declared_roots[root_key]
        if restored != declared_root / relative:
            raise PortabilityError("mapping restored path does not match its root")
        restored_bytes = _read_regular_file(
            restored,
            MAX_CLAIM_BYTES,
            "mapped restored file",
        )
        if _sha256(restored_bytes) != item["sha256"]:
            raise PortabilityError("mapped restored file hash does not match")
        claim_ids.append(item["claim_id"])
    if claim_ids != sorted(claim_ids) or len(claim_ids) != len(set(claim_ids)):
        raise PortabilityError("mapping manifest claims are ambiguous or unordered")
    if _sha256(_canonical_json(claim_ids)) != core["restored_selection_sha256"]:
        raise PortabilityError("mapping restored selection hash does not match")
    if not _is_sha256(manifest["mapping_id"]) or manifest["mapping_id"] != _sha256(
        _canonical_json(core)
    ):
        raise PortabilityError("mapping identity does not match its content")
    return manifest


def _load_verified_bundle(path: str | Path) -> tuple[bytes, dict, list[dict]]:
    supplied = Path(path).expanduser()
    compressed = _read_regular_file(supplied, MAX_BUNDLE_BYTES, "bundle")
    envelope, entries = _decode_bundle(compressed)
    return compressed, envelope, entries


def _admit_duplicate_equivalence(
    seen: dict,
    key: object,
    equivalence: tuple,
    label: str,
) -> None:
    previous = seen.get(key)
    if previous is not None and previous != equivalence:
        raise PortabilityError(f"ambiguous duplicate claims conflict at {label}")
    seen[key] = equivalence


def _coerce_claim(value: FileClaim | Mapping[str, str]) -> FileClaim:
    if isinstance(value, FileClaim):
        claim = value
    elif isinstance(value, Mapping):
        required = {
            "run_id",
            "role",
            "path",
            "historic_locator",
            "expected_sha256",
        }
        if set(value) != required:
            raise PortabilityError(
                "each claim must contain only run_id, role, path, "
                "historic_locator, and expected_sha256"
            )
        if any(not isinstance(value[key], str) for key in required):
            raise PortabilityError("all claim fields must be strings")
        claim = FileClaim(**{key: value[key] for key in required})
    else:
        raise PortabilityError("each claim must be a FileClaim or mapping")
    run_id = claim.run_id.strip()
    if not run_id or len(run_id) > 512 or "\x00" in run_id:
        raise PortabilityError("run_id must be a non-empty bounded string")
    role = claim.role.strip().lower()
    if role not in VALID_ROLES:
        raise PortabilityError(
            f"claim role must be one of: {', '.join(sorted(VALID_ROLES))}"
        )
    locator = claim.historic_locator.strip()
    if not locator:
        raise PortabilityError("historic_locator is required")
    _validate_no_traversal(locator, "historic_locator")
    digest = claim.expected_sha256.strip().lower()
    if not _is_sha256(digest):
        raise PortabilityError("expected_sha256 must be a 64-character SHA-256")
    path = claim.path.strip()
    if not path:
        raise PortabilityError("claim path is required")
    _validate_no_traversal(path, "claim path")
    supplied = Path(path).expanduser()
    if not supplied.is_absolute():
        raise PortabilityError("claim path must be absolute")
    return FileClaim(run_id, role, str(supplied), locator, digest)


def _read_claim(claim: FileClaim) -> tuple[Path, bytes]:
    supplied = Path(claim.path)
    payload = _read_regular_file(supplied, MAX_CLAIM_BYTES, "claimed file")
    resolved = supplied.resolve()
    if _sha256(payload) != claim.expected_sha256:
        raise PortabilityError(f"claimed file hash mismatch: {supplied.name}")
    return resolved, payload


def _read_regular_file(path: Path, limit: int, label: str) -> bytes:
    _reject_symlink_components(path, label)
    if path.is_symlink():
        raise PortabilityError(f"{label} must not be a symlink")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise PortabilityError(f"{label} is missing") from exc
    except OSError as exc:
        raise PortabilityError(f"cannot open {label} safely: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PortabilityError(f"{label} must be a regular file")
        if before.st_size > limit:
            raise PortabilityError(f"{label} exceeds the byte limit")
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise PortabilityError(f"{label} exceeds the byte limit")
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise PortabilityError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _decode_bundle(compressed: bytes) -> tuple[dict, list[dict]]:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as handle:
            encoded = handle.read(MAX_BUNDLE_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise PortabilityError("bundle is not valid gzip") from exc
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise PortabilityError("decompressed bundle exceeds the byte limit")
    if compressed != _deterministic_gzip(encoded):
        raise PortabilityError("bundle gzip is not the canonical deterministic encoding")
    try:
        envelope = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortabilityError("bundle payload is not valid UTF-8 JSON") from exc
    if _canonical_json(envelope) != encoded:
        raise PortabilityError("bundle JSON is not canonical")
    if not isinstance(envelope, dict) or set(envelope) != {
        "schema_version",
        "bundle_id",
        "bundle",
    }:
        raise PortabilityError("bundle envelope shape is invalid")
    if envelope["schema_version"] != BUNDLE_SCHEMA:
        raise PortabilityError("bundle schema is unsupported")
    core = envelope["bundle"]
    if not isinstance(core, dict) or set(core) != {
        "schema_version",
        "version",
        "selection_sha256",
        "a7_code_provenance",
        "claims",
    }:
        raise PortabilityError("bundle core shape is invalid")
    if (
        core["schema_version"] != BUNDLE_CORE_SCHEMA
        or type(core["version"]) is not int
        or core["version"] != IMPLEMENTATION_VERSION
    ):
        raise PortabilityError("bundle core version is unsupported")
    if not _is_sha256(envelope["bundle_id"]):
        raise PortabilityError("bundle identity is invalid")
    if _sha256(_canonical_json(core)) != envelope["bundle_id"]:
        raise PortabilityError("bundle identity does not match its content")
    _validate_provenance(core["a7_code_provenance"])
    claims = core["claims"]
    if not isinstance(claims, list) or not claims:
        raise PortabilityError("bundle must contain at least one claim")
    validated = [_validate_entry(entry) for entry in claims]
    if validated != sorted(validated, key=lambda item: item["claim_id"]):
        raise PortabilityError("bundle claims are not in canonical order")
    _validate_entry_ambiguity(validated)
    if _selection_sha256(validated) != core["selection_sha256"]:
        raise PortabilityError("bundle selection hash does not match its claims")
    return envelope, validated


def _validate_entry(entry: object) -> dict:
    required = {
        "schema_version",
        "claim_id",
        "run_id",
        "role",
        "historic_locator",
        "original_name",
        "expected_sha256",
        "payload",
    }
    if not isinstance(entry, dict) or set(entry) != required:
        raise PortabilityError("bundle claim shape is invalid")
    if (
        entry["schema_version"] != CLAIM_SCHEMA
        or not isinstance(entry["role"], str)
        or entry["role"] not in VALID_ROLES
    ):
        raise PortabilityError("bundle claim schema or role is invalid")
    if (
        not isinstance(entry["run_id"], str)
        or not entry["run_id"]
        or len(entry["run_id"]) > 512
        or "\x00" in entry["run_id"]
    ):
        raise PortabilityError("bundle claim run identity is invalid")
    if not isinstance(entry["historic_locator"], str) or not entry["historic_locator"]:
        raise PortabilityError("bundle historic locator is invalid")
    _validate_no_traversal(entry["historic_locator"], "bundle historic locator")
    name = entry["original_name"]
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise PortabilityError("bundle original_name is unsafe")
    if not _is_sha256(entry["expected_sha256"]):
        raise PortabilityError("bundle expected SHA-256 is invalid")
    identity = {
        "run_id": entry["run_id"],
        "role": entry["role"],
        "historic_locator": entry["historic_locator"],
        "expected_sha256": entry["expected_sha256"],
    }
    if entry["claim_id"] != _sha256(_canonical_json(identity)):
        raise PortabilityError("bundle claim identity does not match its metadata")
    _payload_bytes(entry)
    return entry


def _payload_bytes(entry: dict) -> bytes:
    payload = entry.get("payload")
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "version",
        "encoding",
        "size_bytes",
        "sha256",
        "data",
    }:
        raise PortabilityError("bundle payload shape is invalid")
    if (
        payload["schema_version"] != PAYLOAD_SCHEMA
        or payload["version"] != IMPLEMENTATION_VERSION
        or payload["encoding"] != "base64"
    ):
        raise PortabilityError("bundle payload schema or encoding is unsupported")
    if (
        type(payload["size_bytes"]) is not int
        or not 0 <= payload["size_bytes"] <= MAX_CLAIM_BYTES
    ):
        raise PortabilityError("bundle payload size is invalid")
    if not _is_sha256(payload["sha256"]):
        raise PortabilityError("bundle payload SHA-256 is invalid")
    if not isinstance(payload["data"], str):
        raise PortabilityError("bundle payload data is invalid")
    try:
        data = base64.b64decode(payload["data"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PortabilityError("bundle payload is not valid base64") from exc
    if len(data) != payload["size_bytes"]:
        raise PortabilityError("bundle payload size does not match embedded bytes")
    actual = _sha256(data)
    if actual != payload["sha256"] or actual != entry["expected_sha256"]:
        raise PortabilityError("bundle payload SHA-256 does not match embedded bytes")
    return data


def _validate_entry_ambiguity(entries: list[dict]) -> None:
    ids = set()
    locators = {}
    destinations = {}
    for entry in entries:
        claim_id = entry["claim_id"]
        destination = (
            "intake" if entry["role"] == "raw" else "artifact",
            entry["original_name"],
        )
        if claim_id in ids:
            raise PortabilityError("bundle contains an ambiguous duplicate claim identity")
        _admit_duplicate_equivalence(
            locators,
            entry["historic_locator"],
            (entry["role"], entry["expected_sha256"], destination),
            "historic locator",
        )
        _admit_duplicate_equivalence(
            destinations,
            destination,
            (
                entry["role"],
                entry["historic_locator"],
                entry["expected_sha256"],
            ),
            "restore destination",
        )
        ids.add(claim_id)


def _selection_sha256(entries: list[dict]) -> str:
    selection = [
        {
            "claim_id": entry["claim_id"],
            "run_id": entry["run_id"],
            "role": entry["role"],
            "historic_locator": entry["historic_locator"],
            "original_name": entry["original_name"],
            "expected_sha256": entry["expected_sha256"],
        }
        for entry in entries
    ]
    return _sha256(_canonical_json(selection))


def _a7_code_provenance() -> dict:
    root = Path(__file__).resolve().parent
    head = _git_output(root, ["rev-parse", "HEAD"])
    status = _git_output(root, ["status", "--porcelain", "--", *OWNER_CODE_FILES])
    if head is None or status is None:
        state = "unresolved"
    else:
        state = "dirty" if status else "clean"
    code_hashes = {
        name: _sha256((root / name).read_bytes())
        for name in OWNER_CODE_FILES
        if (root / name).is_file()
    }
    return {
        "schema_version": PROVENANCE_SCHEMA,
        "version": IMPLEMENTATION_VERSION,
        "owner_repository": OWNER_REPOSITORY,
        "owner_commit": head if state == "clean" else None,
        "owner_head_commit": head,
        "owner_worktree_state": state,
        "owner_code_sha256": code_hashes,
    }


def _validate_provenance(value: object) -> None:
    required = {
        "schema_version",
        "version",
        "owner_repository",
        "owner_commit",
        "owner_head_commit",
        "owner_worktree_state",
        "owner_code_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PortabilityError("A7 code provenance shape is invalid")
    if (
        value["schema_version"] != PROVENANCE_SCHEMA
        or type(value["version"]) is not int
        or value["version"] != IMPLEMENTATION_VERSION
        or value["owner_repository"] != OWNER_REPOSITORY
    ):
        raise PortabilityError("A7 code provenance version is unsupported")
    state = value["owner_worktree_state"]
    if not isinstance(state, str) or state not in {"clean", "dirty", "unresolved"}:
        raise PortabilityError("A7 code provenance state is invalid")
    hashes = value["owner_code_sha256"]
    if not isinstance(hashes, dict) or set(hashes) != set(OWNER_CODE_FILES):
        raise PortabilityError("A7 code provenance hashes are missing")
    if any(not isinstance(name, str) or not _is_sha256(digest) for name, digest in hashes.items()):
        raise PortabilityError("A7 code provenance hashes are invalid")
    for key in ("owner_commit", "owner_head_commit"):
        if value[key] is not None and not _is_hex(value[key], 40):
            raise PortabilityError("A7 code provenance commit is invalid")
    owner_commit = value["owner_commit"]
    head = value["owner_head_commit"]
    if state == "clean" and (head is None or owner_commit != head):
        raise PortabilityError("clean A7 provenance must bind the owner commit")
    if state == "dirty" and (head is None or owner_commit is not None):
        raise PortabilityError("dirty A7 provenance must bind only the owner HEAD")
    if state == "unresolved" and owner_commit is not None:
        raise PortabilityError("unresolved A7 provenance cannot claim an owner commit")


def _git_output(root: Path, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _deterministic_gzip(payload: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=output,
        mtime=0,
    ) as handle:
        handle.write(payload)
    return output.getvalue()


def _prepare_root(value: str | Path, label: str) -> Path:
    supplied = Path(value).expanduser()
    _reject_symlink_components(supplied, label)
    if supplied.is_symlink():
        raise PortabilityError(f"{label} must not be a symlink")
    try:
        supplied.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PortabilityError(f"cannot create {label}: {exc}") from exc
    _reject_symlink_components(supplied, label)
    resolved = supplied.resolve()
    if not resolved.is_dir():
        raise PortabilityError(f"{label} must be a directory")
    return resolved


def _prepare_child_directory(root: Path, name: str, *, private: bool) -> Path:
    target = root / name
    _reject_symlink_components(target, name)
    if target.is_symlink():
        raise PortabilityError(f"{name} must not be a symlink")
    try:
        target.mkdir(mode=0o700 if private else 0o755, exist_ok=True)
    except OSError as exc:
        raise PortabilityError(f"cannot create {name}: {exc}") from exc
    _reject_symlink_components(target, name)
    if not target.is_dir():
        raise PortabilityError(f"{name} must be a directory")
    if private and stat.S_IMODE(target.stat().st_mode) & 0o077:
        raise PortabilityError(f"{name} permissions must be private")
    return target


def _preflight_target(path: Path, expected_sha256: str) -> None:
    if path.is_symlink():
        raise PortabilityError(f"restore target must not be a symlink: {path.name}")
    if not path.exists():
        return
    existing = _read_regular_file(path, MAX_CLAIM_BYTES, "restore target")
    if _sha256(existing) != expected_sha256:
        raise PortabilityError(
            f"restore target already contains conflicting bytes: {path.name}"
        )


def _publish_immutable(path: Path, payload: bytes, expected_sha256: str) -> None:
    if _sha256(payload) != expected_sha256:
        raise PortabilityError("internal publish hash does not match payload")
    if path.is_symlink():
        raise PortabilityError(f"immutable target must not be a symlink: {path.name}")
    if path.exists():
        _preflight_target(path, expected_sha256)
        return
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            _preflight_target(path, expected_sha256)
        if _sha256(_read_regular_file(path, len(payload), "immutable target")) != expected_sha256:
            raise PortabilityError(f"immutable target verification failed: {path.name}")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _validate_no_traversal(value: str, label: str) -> None:
    if "\x00" in value:
        raise PortabilityError(f"{label} contains a NUL byte")
    parsed = urllib.parse.urlsplit(value)
    candidate = urllib.parse.unquote(parsed.path if parsed.scheme else value)
    segments = candidate.replace("\\", "/").split("/")
    if ".." in segments:
        raise PortabilityError(f"{label} contains path traversal")


def _reject_symlink_components(path: Path, label: str) -> None:
    """Refuse every existing symlink component without trusting ``resolve``."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for depth, part in enumerate(absolute.parts[1:]):
        current /= part
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise PortabilityError(f"cannot inspect {label} safely: {exc}") from exc
        # macOS exposes standard roots such as /var and /tmp as first-level
        # compatibility symlinks. Trust that platform boundary, but never a
        # symlink introduced below it by a custody caller.
        if depth > 0 and stat.S_ISLNK(current_stat.st_mode):
            raise PortabilityError(f"{label} must not contain a symlink component")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_hex(value: object, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _is_sha256(value: object) -> bool:
    return _is_hex(value, 64)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or restore explicit OPICS document-custody bundles."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser(
        "build",
        help="Build a deterministic selected-file bundle",
    )
    build.add_argument("selection", help="Strict selection JSON path")
    build.add_argument("bundle", help="Exact immutable bundle output path")

    restore = subparsers.add_parser("restore", help="Restore selected bundle claims")
    restore.add_argument("bundle", help="Custody bundle path")
    restore.add_argument("artifact_root", help="Artifact restore root")
    restore.add_argument("intake_root", help="Raw custody restore root")
    restore.add_argument(
        "--manifest",
        required=True,
        help="Exact immutable hash-bound mapping manifest path",
    )
    restore.add_argument(
        "--claim-id",
        action="append",
        dest="claim_ids",
        help="Restore only this claim identity; repeat for multiple claims",
    )

    verify = subparsers.add_parser("verify", help="Validate a custody bundle")
    verify.add_argument("bundle", help="Custody bundle path")

    mapping = subparsers.add_parser(
        "mapping",
        help="Validate and emit a hash-bound restore mapping",
    )
    mapping.add_argument("manifest", help="Restore mapping manifest path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_bundle(
                load_selection(args.selection),
                bundle_path=args.bundle,
            )
            payload = asdict(result)
        elif args.command == "restore":
            result = restore_bundle(
                args.bundle,
                args.artifact_root,
                args.intake_root,
                selected_claim_ids=args.claim_ids,
                mapping_manifest_path=args.manifest,
            )
            payload = asdict(result)
        elif args.command == "verify":
            payload = asdict(verify_bundle(args.bundle))
        else:
            payload = load_mapping_manifest(args.manifest)
    except (OSError, PortabilityError, TypeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
