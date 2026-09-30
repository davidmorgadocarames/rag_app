"""Blob side of the backup Job (T11.2.10, Blob mode of ``scripts/db/backup.sh``).

    pg_dump … | age -r <public key>
        | python -m rag_app.backup_blob upload --name backups/secrag-<ts>.dump.age
    python -m rag_app.backup_blob check

- ``upload`` streams stdin to a NEW blob (never overwrites), refusing anything that does not
  start with the ``age`` header — only ciphertext reaches Azure; the private key never does
  (R4-1).
- ``check`` is the daily oldest-blob check (X9): it fails when there is no backup blob or the
  oldest one under ``backups/`` is older than ``BACKUP_RETENTION_DAYS`` (the Storage lifecycle
  rule deletes them after 12 days, so an older blob means the rule is broken).

Credentials: the Job's user-assigned managed identity (``AZURE_CLIENT_ID``; Storage Blob Data
Contributor on the account; shared-key access is disabled). Settings: ``BACKUP_STORAGE_ACCOUNT``
(required), ``BACKUP_CONTAINER`` (default ``secrag-backups``). The Azure SDK is only in the
slim jobs image; tests pass a fake container client.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from collections.abc import Iterable, Iterator
from typing import Any, BinaryIO, Protocol

from rag_app.retention import BACKUP_RETENTION_DAYS

BACKUP_PREFIX = "backups/"
TOMBSTONE_PREFIX = "tombstones/"
DEFAULT_CONTAINER = "secrag-backups"
AGE_HEADER = b"age-encryption.org/v1\n"
BLOB_NAME_RE = re.compile(r"^backups/secrag-(\d{8}T\d{6}Z)\.dump\.age$")
CHUNK = 4 * 1024 * 1024


class BackupError(RuntimeError):
    """A backup rule failed (no key material or data in the message)."""


class ContainerClient(Protocol):
    """The subset of ``azure.storage.blob.ContainerClient`` used here."""

    def upload_blob(self, name: str, data: Iterable[bytes], overwrite: bool = ...) -> Any: ...

    def get_blob_client(self, blob: str) -> Any: ...

    def list_blobs(self, name_starts_with: str | None = ...) -> Iterable[Any]: ...


def container_client() -> ContainerClient:
    """The real client: managed identity → Blob service → container (jobs image only)."""
    account = os.environ.get("BACKUP_STORAGE_ACCOUNT", "")
    client_id = os.environ.get("AZURE_CLIENT_ID", "")
    if not re.fullmatch(r"[a-z0-9]{3,24}", account):
        raise BackupError("BACKUP_STORAGE_ACCOUNT is missing or not a storage account name")
    if not client_id:
        raise BackupError("AZURE_CLIENT_ID (the Jobs' user-assigned identity) is not set")
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient(
        f"https://{account}.blob.core.windows.net",
        credential=ManagedIdentityCredential(client_id=client_id),
    )
    client: ContainerClient = service.get_container_client(
        os.environ.get("BACKUP_CONTAINER", DEFAULT_CONTAINER)
    )
    return client


def _chunks(first: bytes, stream: BinaryIO) -> Iterator[bytes]:
    yield first
    while chunk := stream.read(CHUNK):
        yield chunk


def upload(client: ContainerClient, name: str, stream: BinaryIO) -> int:
    """Upload an age-encrypted dump from ``stream`` to a new blob; returns its size."""
    if not BLOB_NAME_RE.match(name):
        raise BackupError("blob name must be backups/secrag-<yyyymmddThhmmssZ>.dump.age")
    first = stream.read(len(AGE_HEADER))
    if first != AGE_HEADER:
        raise BackupError("the input is not age-encrypted — refusing to upload it")
    client.upload_blob(name, _chunks(first, stream), overwrite=False)
    size = int(client.get_blob_client(name).get_blob_properties().size)
    if size <= len(AGE_HEADER):
        raise BackupError("the uploaded blob is empty")
    return size


def check(
    client: ContainerClient,
    now: dt.datetime | None = None,
    max_age_days: int = BACKUP_RETENTION_DAYS,
) -> tuple[int, float, float]:
    """(count, oldest age in days, newest age in days) of the backup blobs; raises when there
    is none or the oldest is older than ``max_age_days`` (X9)."""
    now = now or dt.datetime.now(dt.UTC)
    ages = [
        (now - blob.creation_time).total_seconds() / 86400
        for blob in client.list_blobs(name_starts_with=BACKUP_PREFIX)
        if BLOB_NAME_RE.match(blob.name)
    ]
    if not ages:
        raise BackupError("no backup blob under backups/")
    oldest, newest = max(ages), min(ages)
    if oldest > max_age_days:
        raise BackupError(
            f"the oldest backup blob is {oldest:.1f} days old, over the {max_age_days}-day"
            " retention promise — check the Storage lifecycle rule (12 days, backups/)"
        )
    return len(ages), oldest, newest


def main(argv: list[str] | None = None, client: ContainerClient | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_app.backup_blob")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("upload").add_argument("--name", required=True)
    sub.add_parser("check")
    args = parser.parse_args(argv)
    try:
        client = client or container_client()
        if args.cmd == "upload":
            size = upload(client, args.name, sys.stdin.buffer)
            print(f"backup_blob: uploaded {args.name} ({size} bytes, age-encrypted)")
        else:
            count, oldest, newest = check(client)
            print(
                f"backup_blob: {count} backup blob(s); oldest {oldest:.1f} days,"
                f" newest {newest:.1f} days (limit {BACKUP_RETENTION_DAYS} days)"
            )
        return 0
    except BackupError as exc:
        print(f"backup_blob: FAIL — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - SDK errors: class name only
        print(f"backup_blob: FAIL — {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
