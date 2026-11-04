import json
from pathlib import Path

import pytest

from scripts.run_acceptance import select_from_manifest


class Dataset:
    rows = [{"videoID": f"v{i}"} for i in range(1, 21)]


def save(path, samples):
    path.write_text(json.dumps({"samples": samples}), encoding="utf-8")


def test_select_range_preserves_original_indices(tmp_path):
    path = tmp_path / "manifest.json"
    save(path, [{"index": i, "video_id": f"v{i}", "category": "分类"} for i in range(1, 21)])
    selected, provenance = select_from_manifest(path, 8, 20, Dataset())
    assert [item[0] for item in selected] == list(range(8, 21))
    assert [item[2]["videoID"] for item in selected] == [f"v{i}" for i in range(8, 21)]
    assert provenance["non_continuous_experiment"] is True
    assert len(provenance["source_manifest_sha256"]) == 64


@pytest.mark.parametrize("samples,start,end", [
    ([{"index": 8, "video_id": "v8", "category": "x"}], 8, 9),
    ([{"index": 8, "video_id": "v8", "category": "x"}] * 2, 8, 8),
    ([{"index": 8, "video_id": "v8", "category": "x"}, {"index": 9, "video_id": "v8", "category": "x"}], 8, 9),
    ([{"index": 8, "video_id": "unknown", "category": "x"}], 8, 8),
    ([{"index": 8, "video_id": "v8", "category": "x"}], 9, 8),
])
def test_invalid_selection_is_rejected(tmp_path, samples, start, end):
    path = tmp_path / "manifest.json"
    save(path, samples)
    with pytest.raises(ValueError):
        select_from_manifest(path, start, end, Dataset())
