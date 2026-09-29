"""Content-addressed object storage and atomic JSON ledger writes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


class StorageError(Exception):
    """An object or run record is missing, corrupt or unsafe."""


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise StorageError(f"Cannot hash {path}: {exc}") from exc
    return digest.hexdigest(), size


def _atomic_copy(source: Path, target: Path) -> None:
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".tmp-", delete=False) as stream:
            temporary = Path(stream.name)
            with source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, stream, length=1024 * 1024)
        os.replace(temporary, target)
    except OSError as exc:
        raise StorageError(f"Cannot store {source}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, prefix=".tmp-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    except (OSError, ValueError) as exc:
        raise StorageError(f"Cannot write ledger {path}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path):
        for candidate in (root, root / "objects", root / "runs", root / "tmp", root / "cache"):
            if candidate.is_symlink():
                raise StorageError(f"Store directory is a symbolic link: {candidate}")
        self.root = root
        self.objects = root / "objects"
        self.runs = root / "runs"
        self.temporary = root / "tmp"
        self.cache = root / "cache"

    def object_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise StorageError("Invalid SHA-256 object name")
        prefix = self.objects / digest[:2]
        if prefix.is_symlink():
            raise StorageError(f"Object prefix is a symbolic link: {prefix}")
        return prefix / digest

    def put(self, source: Path) -> dict[str, Any]:
        digest, size = hash_file(source)
        target = self.object_path(digest)
        if target.is_symlink():
            raise StorageError(f"Object is a symbolic link: {digest}")
        if target.exists():
            actual, actual_size = hash_file(target)
            if actual != digest or actual_size != size:
                raise StorageError(f"Corrupt existing object: {digest}")
        else:
            _atomic_copy(source, target)
            actual, actual_size = hash_file(target)
            if actual != digest or actual_size != size:
                target.unlink(missing_ok=True)
                raise StorageError(f"Source changed while storing object: {source}")
        return {"sha256": digest, "size": size}

    def check(self, reference: Any) -> list[str]:
        if not isinstance(reference, dict) or set(reference) != {"sha256", "size"}:
            raise StorageError("Malformed object reference")
        digest = reference["sha256"]
        size = reference["size"]
        if type(size) is not int or size < 0:
            raise StorageError("Malformed object size")
        path = self.object_path(digest)
        if path.is_symlink():
            return [f"Object is a symbolic link: {digest}"]
        if not path.is_file():
            return [f"Missing object {digest}"]
        actual, actual_size = hash_file(path)
        if actual != digest or actual_size != size:
            return [f"Corrupt object {digest}"]
        return []

    def materialize(self, reference: dict[str, Any], destination: Path) -> None:
        issues = self.check(reference)
        if issues:
            raise StorageError(issues[0])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.object_path(reference["sha256"]), destination)
