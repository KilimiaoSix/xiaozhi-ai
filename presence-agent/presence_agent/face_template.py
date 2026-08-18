"""Validated, atomic persistence for the local owner face template."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

import numpy as np


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class OwnerTemplate:
    embedding: np.ndarray
    model_sha256: str
    created_at: str
    sample_count: int
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        embedding = np.asarray(self.embedding, dtype=np.float32).copy()
        embedding.setflags(write=False)
        object.__setattr__(self, "embedding", embedding)


def _validated(template: OwnerTemplate) -> OwnerTemplate:
    embedding = np.asarray(template.embedding, dtype=np.float32)
    if embedding.ndim != 1 or embedding.size == 0 or not np.all(np.isfinite(embedding)):
        raise ValueError("template embedding must be a finite one-dimensional vector")
    if not np.isclose(np.linalg.norm(embedding), 1.0, atol=1e-4):
        raise ValueError("template embedding must have unit length")
    if template.schema_version != SCHEMA_VERSION:
        raise ValueError(f"unsupported template schema: {template.schema_version}")
    if (
        len(template.model_sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in template.model_sha256)
    ):
        raise ValueError("model hash must be a 64-character hexadecimal SHA-256")
    if not isinstance(template.created_at, str) or not template.created_at:
        raise ValueError("created_at must be a non-empty string")
    if isinstance(template.sample_count, bool) or template.sample_count <= 0:
        raise ValueError("sample_count must be positive")
    return OwnerTemplate(
        embedding=embedding,
        model_sha256=template.model_sha256.lower(),
        created_at=template.created_at,
        sample_count=template.sample_count,
        schema_version=template.schema_version,
    )


def save_template(path, template: OwnerTemplate, overwrite: bool = False) -> None:
    destination = Path(path)
    validated = _validated(template)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"template already exists: {destination}")

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.stem}-",
            suffix=".npz",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            np.savez(
                temporary,
                embedding=validated.embedding,
                schema_version=np.array(validated.schema_version, dtype=np.int64),
                model_sha256=np.array(validated.model_sha256),
                created_at=np.array(validated.created_at),
                sample_count=np.array(validated.sample_count, dtype=np.int64),
            )
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _scalar(archive, name: str):
    if name not in archive.files:
        raise ValueError(f"template is missing field: {name}")
    value = archive[name]
    if value.ndim != 0:
        raise ValueError(f"template field must be scalar: {name}")
    return value.item()


def load_template(path, expected_model_sha256: str) -> OwnerTemplate:
    with np.load(Path(path), allow_pickle=False) as archive:
        if "embedding" not in archive.files:
            raise ValueError("template is missing field: embedding")
        template = OwnerTemplate(
            embedding=np.asarray(archive["embedding"], dtype=np.float32),
            schema_version=int(_scalar(archive, "schema_version")),
            model_sha256=str(_scalar(archive, "model_sha256")),
            created_at=str(_scalar(archive, "created_at")),
            sample_count=int(_scalar(archive, "sample_count")),
        )
    validated = _validated(template)
    if validated.model_sha256 != expected_model_sha256.lower():
        raise ValueError("template model hash does not match the configured recognition model")
    return validated


def delete_template(path) -> bool:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    return True


def save_metadata(path, value: dict) -> None:
    destination = Path(path)
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix=f".{destination.stem}-",
            suffix=".json",
            dir=destination.parent,
            delete=False,
            encoding="utf-8",
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(value, temporary, ensure_ascii=False, separators=(",", ":"))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_metadata(path) -> dict:
    with Path(path).open("r", encoding="utf-8") as metadata_file:
        value = json.load(metadata_file)
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    return value
