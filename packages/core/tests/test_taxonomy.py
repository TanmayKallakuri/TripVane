import hashlib
from pathlib import Path

import pytest

from tripvane_core.taxonomy import DEFAULT_PATH, load_taxonomy


def test_repository_taxonomy_has_the_three_axes() -> None:
    taxonomy = load_taxonomy()
    assert taxonomy.version == hashlib.sha256(DEFAULT_PATH.read_bytes()).hexdigest()
    assert taxonomy.tags("technique") == [
        "direct_instruction",
        "role_override",
        "hidden_in_document",
        "tool_argument_injection",
        "encoded",
        "other",
    ]
    assert taxonomy.tags("objective") == [
        "exfiltrate_secrets",
        "send_message",
        "destructive_action",
        "persistence",
        "recon",
        "none",
    ]
    assert taxonomy.tags("target_tool") == [
        "email",
        "filesystem",
        "shell",
        "http",
        "secrets",
        "none",
    ]
    assert list(taxonomy.axes) == ["technique", "objective", "target_tool"]
    for tags in taxonomy.axes.values():
        assert all(description.endswith(".") for description in tags.values())


def test_version_changes_with_any_edit(tmp_path: Path) -> None:
    path = tmp_path / "tags.yaml"
    path.write_text("version: 1\naxes:\n  a:\n    x: An x.\n", encoding="utf-8")
    first = load_taxonomy(path).version
    path.write_text("# note\nversion: 1\naxes:\n  a:\n    x: An x.\n", encoding="utf-8")
    assert load_taxonomy(path).version != first


@pytest.mark.parametrize(
    "text",
    [
        "axes:\n  a:\n    x: An x.\n",
        "version: 1\naxes: {}\n",
        "version: 1\naxes:\n  a: {}\n",
        "version: 1\naxes:\n  a:\n    x: ''\n",
    ],
)
def test_malformed_files_are_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "tags.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_taxonomy(path)
