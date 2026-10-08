"""Static responses for the infrastructure lookalikes, in each product's documented shape.

Nothing here is computed from a request. Model digests, ids and timestamps are fixed
made-up values.
"""

from typing import Any

# Ollama, GET /api/tags: the locally installed models.
OLLAMA_TAGS: dict[str, Any] = {
    "models": [
        {
            "name": "llama3.1:8b",
            "model": "llama3.1:8b",
            "modified_at": "2026-09-21T14:03:11.402711Z",
            "size": 4920753328,
            "digest": "46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e",
            "details": {
                "parent_model": "",
                "format": "gguf",
                "family": "llama",
                "families": ["llama"],
                "parameter_size": "8.0B",
                "quantization_level": "Q4_K_M",
            },
        },
        {
            "name": "qwen2.5-coder:7b",
            "model": "qwen2.5-coder:7b",
            "modified_at": "2026-09-30T08:47:55.118204Z",
            "size": 4683087332,
            "digest": "2b0496514337a3d5901f1d253d01726c890b721e891335a56d6e08cedf3e2cb0",
            "details": {
                "parent_model": "",
                "format": "gguf",
                "family": "qwen2",
                "families": ["qwen2"],
                "parameter_size": "7.6B",
                "quantization_level": "Q4_K_M",
            },
        },
    ]
}

# Ollama, POST /api/generate: a finished, non-streamed generation.
OLLAMA_GENERATE: dict[str, Any] = {
    "model": "llama3.1:8b",
    "created_at": "2026-10-07T19:22:41.538917Z",
    "response": "Hello! How can I help you today?",
    "done": True,
    "done_reason": "stop",
    "context": [128006, 882, 128007, 271, 9906, 0, 2650, 649, 358, 1520, 499, 3432, 30],
    "total_duration": 1840562917,
    "load_duration": 21604583,
    "prompt_eval_count": 26,
    "prompt_eval_duration": 312845000,
    "eval_count": 10,
    "eval_duration": 1503112000,
}

# Open WebUI, GET /api/v1/auths/: the signed-in user. Without a bearer token Open WebUI
# answers 401 with this body.
OPEN_WEBUI_NOT_AUTHENTICATED: dict[str, Any] = {"detail": "Not authenticated"}

# LiteLLM proxy, GET /health: per-deployment health of the configured models.
LITELLM_HEALTH: dict[str, Any] = {
    "healthy_endpoints": [
        {"model": "openai/gpt-4o-mini", "cache": {"no-cache": True}},
        {"model": "openai/gpt-4o", "cache": {"no-cache": True}},
        {"model": "ollama/llama3.1:8b", "api_base": "http://ollama:11434"},
    ],
    "unhealthy_endpoints": [],
    "healthy_count": 3,
    "unhealthy_count": 0,
}

# LiteLLM proxy, GET /v1/models: the OpenAI-compatible model list.
LITELLM_MODELS: dict[str, Any] = {
    "data": [
        {"id": "gpt-4o-mini", "object": "model", "created": 1677610602, "owned_by": "openai"},
        {"id": "gpt-4o", "object": "model", "created": 1677610602, "owned_by": "openai"},
        {"id": "llama3.1-8b", "object": "model", "created": 1677610602, "owned_by": "openai"},
    ],
    "object": "list",
}

# Langflow, GET /api/v1/flows/: the flows visible to the (auto-login) user.
LANGFLOW_FLOWS: list[dict[str, Any]] = [
    {
        "name": "Support Ticket Router",
        "description": "Classifies incoming tickets and drafts a first reply.",
        "icon": "Bot",
        "icon_bg_color": None,
        "gradient": "2",
        "data": {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
        "is_component": False,
        "updated_at": "2026-09-28T16:12:09.774115",
        "webhook": False,
        "endpoint_name": "support-ticket-router",
        "tags": [],
        "locked": False,
        "mcp_enabled": False,
        "action_name": None,
        "action_description": None,
        "access_type": "PRIVATE",
        "id": "6c1f2d0e-8a4b-4c7e-9f15-3b2a7d9e4f61",
        "user_id": "0b9e5a43-21f7-4d6c-a8e2-5f3c1b7d9a20",
        "folder_id": "e4a7c2b9-5d13-4f86-b0e1-9c8d7a6f5b43",
    }
]

# The answer to anything not listed, in the FastAPI shape three of the four products use.
NOT_FOUND: dict[str, Any] = {"detail": "Not Found"}
METHOD_NOT_ALLOWED: dict[str, Any] = {"detail": "Method Not Allowed"}
TOO_LARGE: dict[str, Any] = {"detail": "Request entity too large"}

# (status, body) per path and method. Paths are matched without a trailing slash.
ROUTES: dict[str, dict[str, tuple[int, Any]]] = {
    "/api/tags": {"GET": (200, OLLAMA_TAGS)},
    "/api/generate": {"POST": (200, OLLAMA_GENERATE)},
    "/api/v1/auths": {"GET": (401, OPEN_WEBUI_NOT_AUTHENTICATED)},
    "/health": {"GET": (200, LITELLM_HEALTH)},
    "/v1/models": {"GET": (200, LITELLM_MODELS)},
    "/api/v1/flows": {"GET": (200, LANGFLOW_FLOWS)},
}
