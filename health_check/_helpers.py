"""Shared helpers for health-check diagnostic actions."""
from __future__ import annotations

import json
import traceback
from typing import Any

import azure.functions as func


def json_response(data: dict[str, Any], status_code: int = 200) -> func.HttpResponse:
    """Return a JSON HttpResponse with standard formatting."""
    return func.HttpResponse(
        json.dumps(data, indent=2, default=str),
        status_code=status_code,
        mimetype="application/json",
    )


def error_response(message: str, status_code: int = 400) -> func.HttpResponse:
    """Return a JSON error response."""
    return json_response({"error": message}, status_code=status_code)


def run_diagnostic(fn):
    """Decorator that wraps a diagnostic action with standard error handling.

    The wrapped function receives ``req`` and ``results`` (a dict pre-seeded
    with ``{"steps": []}``).  Any unhandled exception is caught, logged into
    *results*, and returned as a 200 JSON body so the caller always gets
    structured diagnostics.
    """

    def wrapper(req: func.HttpRequest) -> func.HttpResponse:
        results: dict[str, Any] = {"steps": []}
        try:
            fn(req, results)
        except Exception as e:
            results["error"] = str(e)
            results["traceback"] = traceback.format_exc()
            results["steps"].append(f"FAILED: {e}")
        return json_response(results)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper
