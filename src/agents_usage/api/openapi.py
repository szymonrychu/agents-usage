"""Deterministic OpenAPI export: `python -m agents_usage.api.openapi > openapi.json`.

The document covers every route of the service: the read routes (served on the UI and the
service listeners) and the ingest route (served on the ingest listener). FastAPI documents a
validation error as its own `HTTPValidationError`; this service answers every error with
`{"error": {"code", "message"}}` (see `errors.py`), so the export replaces the generated error
responses with that shape and the statuses each route really returns.
"""

from __future__ import annotations

import json
from typing import Any

from agents_usage.api import ingest, usage
from agents_usage.api.app import PREFIX, _build_app
from agents_usage.api.schemas import SampleIn
from agents_usage.settings import Settings

ERROR_REF = {"$ref": "#/components/schemas/ErrorResponse"}

ERROR_SCHEMAS: dict[str, Any] = {
    "ErrorBody": {
        "type": "object",
        "title": "ErrorBody",
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string", "title": "Code"},
            "message": {"type": "string", "title": "Message"},
        },
    },
    "ErrorResponse": {
        "type": "object",
        "title": "ErrorResponse",
        "required": ["error"],
        "properties": {"error": {"$ref": "#/components/schemas/ErrorBody"}},
    },
}

ERRORS = {
    401: "no or an unknown bearer token on the service or ingest listener (unauthorized)",
    403: "source address is not allowed on the UI listener (forbidden)",
    413: "body exceeds 8192 bytes (too_large)",
    422: "invalid request (invalid_request, no_windows)",
}

ROUTE_ERRORS: dict[tuple[str, str], tuple[int, ...]] = {
    ("get", "/current"): (401, 403),
    ("get", "/history"): (401, 403, 422),
    ("post", "/samples"): (401, 413, 422),
}


def _document() -> dict[str, Any]:
    settings = Settings(database_url="postgresql://openapi/export")
    app = _build_app(settings, pool=None)  # type: ignore[arg-type]
    app.include_router(usage.router, prefix=PREFIX)
    app.include_router(ingest.router, prefix=PREFIX)
    doc = app.openapi()
    for path, methods in doc["paths"].items():
        if not path.startswith(PREFIX):
            continue
        for method, operation in methods.items():
            responses = operation["responses"]
            for status in [s for s in responses if not s.startswith("2")]:
                del responses[status]
            for status in ROUTE_ERRORS[(method, path.removeprefix(PREFIX))]:
                responses[str(status)] = {
                    "description": ERRORS[status],
                    "content": {"application/json": {"schema": ERROR_REF}},
                }
    schemas = doc["components"]["schemas"]
    # The ingest route reads the raw request (to cap the body while streaming), so FastAPI
    # does not see its body or its answer; document both here.
    sample_in = SampleIn.model_json_schema()
    sample_in.pop("$defs", None)
    schemas["SampleIn"] = sample_in
    post = doc["paths"][f"{PREFIX}/samples"]["post"]
    post["requestBody"] = {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SampleIn"}}},
    }
    post["responses"]["202"] = {
        "description": "the sample was stored; the number of windows it held",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "title": "SampleAccepted",
                    "required": ["windows"],
                    "properties": {"windows": {"type": "integer", "title": "Windows"}},
                }
            }
        },
    }
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    schemas.update(ERROR_SCHEMAS)
    return doc


def export() -> str:
    """The app's OpenAPI document as JSON with sorted keys and a trailing newline."""
    return json.dumps(_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(export(), end="")
