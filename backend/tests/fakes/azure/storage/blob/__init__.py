"""FAKE ``azure.storage.blob`` for the backup tests (never on the app's import path).

Blobs are files under ``$FAKE_BLOB_ROOT/<container>/<name>``; a blob's ``creation_time`` is
the file's mtime (tests age a blob with ``os.utime``).
"""

from __future__ import annotations

import datetime as dt
import os
import types
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


class ResourceExistsError(Exception):
    pass


def _root() -> Path:
    return Path(os.environ["FAKE_BLOB_ROOT"])


class _BlobClient:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get_blob_properties(self) -> Any:
        return types.SimpleNamespace(size=self.path.stat().st_size)


class ContainerClient:
    def __init__(self, root: Path) -> None:
        self.root = root

    def upload_blob(self, name: str, data: Iterable[bytes], overwrite: bool = False) -> None:
        path = self.root / name
        if path.exists() and not overwrite:
            raise ResourceExistsError(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            for chunk in data:
                handle.write(chunk)

    def get_blob_client(self, blob: str) -> _BlobClient:
        return _BlobClient(self.root / blob)

    def list_blobs(self, name_starts_with: str | None = None) -> Iterator[Any]:
        for path in sorted(p for p in self.root.rglob("*") if p.is_file()):
            name = path.relative_to(self.root).as_posix()
            if name.startswith(name_starts_with or ""):
                yield types.SimpleNamespace(
                    name=name,
                    creation_time=dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC),
                )


class BlobServiceClient:
    def __init__(self, account_url: str, credential: Any) -> None:
        _root().mkdir(parents=True, exist_ok=True)
        (_root() / "_connection").write_text(
            f"{account_url} {type(credential).__name__} {credential.client_id}\n"
        )

    def get_container_client(self, name: str) -> ContainerClient:
        return ContainerClient(_root() / name)
