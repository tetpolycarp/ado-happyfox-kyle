"""Diagnostic endpoint — health check + manual queue processing test.

Each action is implemented in a dedicated module under health_check/:
  _ado_diagnostics.py   — test_parent, ado_fields, test_mi_auth, ado_picklists, ado_projects
  _hf_diagnostics.py    — hf_info, test_child, test_update
  _queue_diagnostics.py — queue_status, peek_dlq, replay_dlq
  _flow_diagnostics.py  — test_child_flow, test_attachments, test_comments
"""
import json
import os
import sys

import azure.functions as func

from health_check._helpers import json_response
from health_check._ado_diagnostics import test_parent, ado_fields, test_mi_auth, ado_picklists, ado_projects
from health_check._hf_diagnostics import hf_info, test_child, test_update
from health_check._queue_diagnostics import queue_status, peek_dlq, replay_dlq
from health_check._flow_diagnostics import test_child_flow, test_attachments, test_comments

# Action name → handler function.  Each handler has the signature:
#   (req: func.HttpRequest) -> func.HttpResponse
_ACTION_HANDLERS = {
    "test_parent": test_parent,
    "hf_info": hf_info,
    "test_child": test_child,
    "test_update": test_update,
    "test_attachments": test_attachments,
    "queue_status": queue_status,
    "peek_dlq": peek_dlq,
    "replay_dlq": replay_dlq,
    "test_child_flow": test_child_flow,
    "ado_fields": ado_fields,
    "test_comments": test_comments,
    "test_mi_auth": test_mi_auth,
    "ado_picklists": ado_picklists,
    "ado_projects": ado_projects,
}


def main(req: func.HttpRequest) -> func.HttpResponse:
    action = req.params.get("action", "health")

    if action == "health":
        return json_response({
            "status": "ok",
            "python_version": sys.version,
            "cwd": os.getcwd(),
        })

    handler = _ACTION_HANDLERS.get(action)
    if handler is not None:
        return handler(req)

    return func.HttpResponse("Unknown action", status_code=400)
