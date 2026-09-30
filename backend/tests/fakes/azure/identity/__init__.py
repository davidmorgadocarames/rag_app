"""FAKE ``azure.identity`` for the backup tests."""

from __future__ import annotations


class ManagedIdentityCredential:
    def __init__(self, client_id: str | None = None) -> None:
        self.client_id = client_id
