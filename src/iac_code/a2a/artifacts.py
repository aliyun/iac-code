from __future__ import annotations

import base64
import hashlib
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


class UnsafeArtifactNameError(ValueError):
    """Raised when an artifact filename would escape the artifact directory."""


@dataclass(frozen=True)
class A2AArtifactMetadata:
    artifact_id: str
    filename: str
    media_type: str
    byte_size: int
    sha256: str
    uri: str

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        return {
            "artifactId": data["artifact_id"],
            "filename": data["filename"],
            "mediaType": data["media_type"],
            "byteSize": data["byte_size"],
            "sha256": data["sha256"],
            "uri": data["uri"],
        }


class A2AArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save_text(self, *, filename: str, content: str, media_type: str) -> A2AArtifactMetadata:
        encoded = content.encode("utf-8")
        return self.save_bytes(filename=filename, content=encoded, media_type=media_type)

    def save_base64(self, *, filename: str, content: str, media_type: str) -> A2AArtifactMetadata:
        decoded = base64.b64decode(content.encode("ascii"), validate=True)
        return self.save_bytes(filename=filename, content=decoded, media_type=media_type)

    def save_bytes(self, *, filename: str, content: bytes, media_type: str) -> A2AArtifactMetadata:
        safe_name = self._safe_filename(filename)
        artifact_id = str(uuid.uuid4())
        artifact_dir = self.root / artifact_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        path = artifact_dir / safe_name
        path.write_bytes(content)
        return A2AArtifactMetadata(
            artifact_id=artifact_id,
            filename=safe_name,
            media_type=media_type,
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            uri=path.resolve().as_uri(),
        )

    def path_for(self, artifact_id: str) -> Path:
        candidates = list((self.root / artifact_id).iterdir())
        if not candidates:
            raise FileNotFoundError(artifact_id)
        return candidates[0]

    @staticmethod
    def _safe_filename(filename: str) -> str:
        if not filename or filename != os.path.basename(filename) or filename in {".", ".."}:
            raise UnsafeArtifactNameError("Unsafe artifact filename")
        return filename
