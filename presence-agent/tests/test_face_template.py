from dataclasses import replace

import numpy as np
import pytest

from presence_agent.face_template import (
    OwnerTemplate,
    load_metadata,
    load_template,
    save_metadata,
    save_template,
)


MODEL_HASH = "a" * 64


def template(**changes):
    value = OwnerTemplate(
        embedding=np.array([0.6, 0.8], dtype=np.float32),
        model_sha256=MODEL_HASH,
        created_at="2026-08-18T00:00:00Z",
        sample_count=18,
    )
    return replace(value, **changes)


def test_template_round_trip_and_atomic_replace(tmp_path):
    path = tmp_path / "owner_template.npz"
    save_template(path, template())

    loaded = load_template(path, MODEL_HASH)

    np.testing.assert_allclose(loaded.embedding, [0.6, 0.8])
    assert loaded.embedding.flags.writeable is False
    assert loaded.sample_count == 18

    save_template(path, template(sample_count=17), overwrite=True)
    assert load_template(path, MODEL_HASH).sample_count == 17
    assert list(tmp_path.iterdir()) == [path]


def test_template_rejects_model_mismatch_and_pickle(tmp_path):
    path = tmp_path / "owner_template.npz"
    save_template(path, template())

    with pytest.raises(ValueError, match="model hash"):
        load_template(path, "b" * 64)

    np.savez(
        path,
        embedding=np.array([0.6, 0.8], dtype=np.float32),
        schema_version=np.array(1),
        model_sha256=np.array(MODEL_HASH),
        created_at=np.array(object(), dtype=object),
        sample_count=np.array(18),
    )
    with pytest.raises(ValueError, match="Object arrays cannot be loaded"):
        load_template(path, MODEL_HASH)


def test_metadata_round_trip_uses_atomic_json_replace(tmp_path):
    path = tmp_path / "owner.json"
    value = {
        "profile_id": "owner",
        "sample_id": "sample-1",
        "display_name": "主人",
        "stored_at": "2026-08-18T00:00:00Z",
        "sample_count": 18,
    }

    save_metadata(path, value)
    assert load_metadata(path) == value

    replacement = {**value, "sample_id": "sample-2"}
    save_metadata(path, replacement)
    assert load_metadata(path) == replacement
    assert list(tmp_path.iterdir()) == [path]
