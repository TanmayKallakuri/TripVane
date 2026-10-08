"""The tag taxonomy: taxonomy/tags.yaml, its axes and its version hash.

The file has a human-maintained `version` field and an `axes` mapping of axis name to
tag name to a one-sentence description. taxonomy_version, the value stored next to
every gate verdict and payload tag, is the sha256 of the file's bytes, so any change to
the file marks earlier results as stale.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# taxonomy/ sits at the repository root; the analyst runs from a checkout.
DEFAULT_PATH = Path(__file__).resolve().parents[4] / "taxonomy" / "tags.yaml"


@dataclass(frozen=True)
class Taxonomy:
    # sha256 hex of the file contents.
    version: str
    # axis -> tag name -> description, in file order.
    axes: dict[str, dict[str, str]]

    def tags(self, axis: str) -> list[str]:
        return list(self.axes[axis])


def load_taxonomy(path: Path = DEFAULT_PATH) -> Taxonomy:
    raw = path.read_bytes()
    data: Any = yaml.safe_load(raw)
    if not isinstance(data, dict) or not isinstance(data.get("version"), int):
        raise ValueError(f"{path}: expected a mapping with an integer version field")
    axes = data.get("axes")
    if not isinstance(axes, dict) or not axes:
        raise ValueError(f"{path}: expected a non-empty axes mapping")
    for axis, tags in axes.items():
        if not isinstance(tags, dict) or not tags:
            raise ValueError(f"{path}: axis {axis!r} must map tag names to descriptions")
        for name, description in tags.items():
            if not isinstance(name, str) or not isinstance(description, str) or not description:
                raise ValueError(f"{path}: tag {axis}/{name} needs a one-sentence description")
    return Taxonomy(version=hashlib.sha256(raw).hexdigest(), axes=axes)
