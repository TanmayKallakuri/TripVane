"""The triage bot's tools: the standard decoy tools plus add_label and post_comment.

add_label and post_comment are decoys like every other tool here. They never call
GitHub; the sensor's GitHub App has no write permission at all. The model sees a
plausible confirmation and the attempt is recorded.
"""

from typing import Any

from tripvane_sensors.runtime.tools import STANDARD_TOOLS, DecoyTool


def _label_added(arguments: dict[str, Any]) -> str:
    return f"Label {str(arguments.get('label', ''))!r} added."


def _comment_posted(arguments: dict[str, Any]) -> str:
    return "Comment posted (id 2841907316)."


ADD_LABEL = DecoyTool(
    name="add_label",
    description="Add a label to the issue or pull request being triaged.",
    input_schema={
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    },
    fake_result=_label_added,
)

POST_COMMENT = DecoyTool(
    name="post_comment",
    description="Post a comment on the issue or pull request being triaged.",
    input_schema={
        "type": "object",
        "properties": {"body": {"type": "string"}},
        "required": ["body"],
    },
    fake_result=_comment_posted,
)

GITHUB_TOOLS: tuple[DecoyTool, ...] = (*STANDARD_TOOLS, ADD_LABEL, POST_COMMENT)
