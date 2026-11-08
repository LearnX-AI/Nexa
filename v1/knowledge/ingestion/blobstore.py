"""Content-addressed store for original files (NEXA-M2-002).

Originals are kept by checksum so a failed job can be retried, and a document
can be reprocessed with a newer pipeline, without asking for the upload again.
Files are written once and never modified. DEV and QA use a local directory;
production points the same interface at object storage.
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional


class BlobStore:
    def __init__(self, root: str = "") -> None:
        self.root = root
        self._mem: dict[str, bytes] = {}
        if root:
            os.makedirs(root, mode=0o750, exist_ok=True)

    def _path(self, checksum: str) -> str:
        hexd = checksum.split(":", 1)[1]
        return os.path.join(self.root, hexd[:2], hexd)

    def put(self, checksum: str, data: bytes) -> str:
        if not self.root:
            self._mem[checksum] = data
            return f"blob://sha256/{checksum.split(':', 1)[1]}"
        path = self._path(checksum)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o440)
            os.replace(tmp, path)
        return f"blob://sha256/{checksum.split(':', 1)[1]}"

    def get(self, checksum: str) -> Optional[bytes]:
        if not self.root:
            return self._mem.get(checksum)
        path = self._path(checksum)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()
