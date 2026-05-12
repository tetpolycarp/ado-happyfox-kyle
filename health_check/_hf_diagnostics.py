"""HappyFox-focused diagnostic actions: hf_info, test_child, test_update."""
from __future__ import annotations

import traceback
from typing import Any

import azure.functions as func

from health_check._helpers import error_response, run_diagnostic


# ---------------------------------------------------------------------------
# hf_info
# ---------------------------------------------------------------------------
@run_diagnostic
def hf_info(req: func.HttpRequest, results: dict[str, Any]) -> None:
    """Query HappyFox for custom fields, categories, statuses, priorities, and contacts."""
    import httpx
    from src.integration.config import settings

    base_url = settings.happyfox_api_url.rstrip("/")
    auth = (settings.happyfox_api_key.get_secret_value(), settings.happyfox_auth_code.get_secret_value())
    client = httpx.Client(auth=auth, timeout=30.0)

    for endpoint, key, transform in [
        ("ticket_custom_fields", "custom_fields", None),
        ("categories", "categories", lambda data: [{"id": c["id"], "name": c["name"]} for c in data]),
        ("statuses", "statuses", None),
        ("priorities", "priorities", None),
    ]:
        resp = client.get(f"{base_url}/{endpoint}/")
        if resp.is_success:
            results[key] = transform(resp.json()) if transform else resp.json()
        else:
            results[f"{key}_error"] = f"{resp.status_code}: {resp.text[:300]}"

    # Search for contact "Avaratak"
    resp = client.get(f"{base_url}/users/", params={"q": "Avaratak", "size": 5})
    if resp.is_success:
        results["contact_search"] = resp.json()
    else:
        results["contact_search_error"] = f"{resp.status_code}: {resp.text[:300]}"

    client.close()


# ---------------------------------------------------------------------------
# test_child
# ---------------------------------------------------------------------------
def test_child(req: func.HttpRequest) -> func.HttpResponse:
    """Test the child→HF flow for a specific ADO child work item."""
    child_id_param = req.params.get("child_id", "")
    if not child_id_param:
        return error_response("child_id parameter required. Usage: ?action=test_child&child_id=12345&category=2")

    category_override = req.params.get("category", "2")
    try:
        category_id = int(category_override)
    except ValueError:
        category_id = 2

    results: dict[str, Any] = {"steps": [], "child_id": child_id_param, "category_id": category_id}
    try:
        _run_test_child(req, results, child_id_param, category_id)
    except Exception as e:
        results["error"] = str(e)
        results["traceback"] = traceback.format_exc()
        results["steps"].append(f"FAILED: {e}")

    from health_check._helpers import json_response
    return json_response(results)


def _run_test_child(req: func.HttpRequest, results: dict[str, Any], child_id_param: str, category_id: int) -> None:
    results["steps"].append("Importing modules...")
    from src.integration.config import settings
    from src.integration.services.ado_service import AdoService
    from src.integration.services.happyfox_service import HappyFoxService
    from src.integration.services.transform_service import ado_client_story_to_happyfox_create
    from src.integration.models.ado_models import AdoFieldNames
    results["steps"].append("OK")

    results["steps"].append(f"Fetching ADO work item {child_id_param}...")
    ado = AdoService(settings)
    hf = HappyFoxService(settings)
    try:
        work_item = ado.get_work_item(int(child_id_param))
        fields = work_item.get("fields", {})
        results["title"] = fields.get("System.Title", "")
        results["client"] = fields.get(AdoFieldNames.CLIENT_REQUESTED, "")
        results["priority"] = fields.get(AdoFieldNames.PRIORITY, "")
        results["steps"].append(f"Got: {fields.get('System.Title', '')}")

        results["steps"].append(f"Building HF create payload (category={category_id})...")
        parent_id = fields.get(AdoFieldNames.ADO_PARENT_ID, "")
        if not parent_id:
            resolved = ado.get_parent_id(int(child_id_param))
            parent_id = str(resolved) if resolved else "unknown"
            results["steps"].append(f"Resolved parent from hierarchy: {parent_id}")

        # Resolve parent title for HF "Parent" field
        parent_title = ""
        if parent_id and parent_id != "unknown":
            try:
                parent_wi = ado.get_work_item(int(parent_id))
                parent_title = parent_wi.get("fields", {}).get("System.Title", "")
            except Exception:
                pass

        create_payload = ado_client_story_to_happyfox_create(
            fields=fields,
            hf_service=hf,
            category_id=category_id,
            ado_parent_title=parent_title,
            ado_child_id=child_id_param,
        )
        create_payload.email = settings.hf_create_user_email
        create_payload.name = settings.hf_create_user_name

        api_payload = create_payload.to_api_payload()
        results["api_payload"] = api_payload
        results["steps"].append("Payload built, sending to HF...")

        import httpx
        base_url = settings.happyfox_api_url.rstrip("/")
        auth = (settings.happyfox_api_key.get_secret_value(), settings.happyfox_auth_code.get_secret_value())
        hf_client = httpx.Client(auth=auth, timeout=30.0)

        cat_resp = hf_client.get(f"{base_url}/categories/")
        if cat_resp.is_success:
            results["available_categories"] = cat_resp.json()
        results["base_url"] = base_url

        test_email = req.params.get("email", settings.hf_create_user_email)
        test_name = req.params.get("name", settings.hf_create_user_name)
        results["contact_email"] = test_email

        attempts = [
            {"label": "category=2 (int)", "category": 2},
            {"label": "category='2' (string)", "category": "2"},
            {"label": "category='Development' (name)", "category": "Development"},
        ]
        results["category_attempts"] = []

        for attempt in attempts:
            test_payload = {
                "subject": "Test ticket",
                "text": "Test",
                "category": attempt["category"],
                "priority": 4,
                "email": test_email,
                "name": test_name,
                "t-cf-8": 69,
                "t-cf-5": 62,
            }
            resp = hf_client.post(f"{base_url}/tickets/", json=test_payload)
            result = {"label": attempt["label"], "status": resp.status_code, "response": resp.text[:500]}
            results["category_attempts"].append(result)

            if resp.is_success:
                hf_ticket = resp.json()
                results["hf_ticket_id"] = hf_ticket.get("id")
                results["steps"].append(f"SUCCESS with {attempt['label']}! Ticket: {hf_ticket.get('id')}")
                break
            else:
                results["steps"].append(f"{attempt['label']}: {resp.status_code}")

        if "hf_ticket_id" not in results:
            results["steps"].append("All category formats failed. Checking ticket form...")
            form_resp = hf_client.get(f"{base_url}/new_ticket_form/")
            results["new_ticket_form_status"] = form_resp.status_code
            results["new_ticket_form"] = form_resp.text[:3000]

        hf_client.close()

    finally:
        ado.close()
        hf.close()


# ---------------------------------------------------------------------------
# test_update
# ---------------------------------------------------------------------------
def test_update(req: func.HttpRequest) -> func.HttpResponse:
    """Test updating a HappyFox ticket's custom fields."""
    ticket_id_param = req.params.get("ticket_id", "")
    if not ticket_id_param:
        return error_response("ticket_id required. Usage: ?action=test_update&ticket_id=665&t-cf-2=20")

    results: dict[str, Any] = {"steps": [], "ticket_id": ticket_id_param}
    try:
        _run_test_update(req, results, ticket_id_param)
    except Exception as e:
        results["error"] = str(e)
        results["traceback"] = traceback.format_exc()
        results["steps"].append(f"FAILED: {e}")

    from health_check._helpers import json_response
    return json_response(results)


def _run_test_update(req: func.HttpRequest, results: dict[str, Any], ticket_id_param: str) -> None:
    import httpx
    from src.integration.config import settings

    base_url = settings.happyfox_api_url.rstrip("/")
    auth = (settings.happyfox_api_key.get_secret_value(), settings.happyfox_auth_code.get_secret_value())
    client = httpx.Client(auth=auth, timeout=30.0)

    results["steps"].append("Fetching staff list...")
    staff_resp = client.get(f"{base_url}/staff/")
    if staff_resp.is_success:
        staff_list = staff_resp.json()
        staff_id = staff_list[0]["id"] if staff_list else None
        results["staff_id"] = staff_id
        results["staff_count"] = len(staff_list)
        results["steps"].append(f"Staff ID: {staff_id}")
    else:
        staff_id = None
        results["steps"].append(f"Staff fetch failed: {staff_resp.status_code}")

    payload: dict[str, Any] = {"text": "Test update from diagnostic endpoint"}
    if staff_id:
        payload["staff"] = staff_id

    for key in req.params:
        if key.startswith("t-cf-"):
            val = req.params[key]
            try:
                payload[key] = int(val)
            except ValueError:
                payload[key] = val

    if req.params.get("subject"):
        payload["subject"] = req.params["subject"]
    if req.params.get("priority"):
        payload["priority"] = int(req.params["priority"])

    results["update_payload"] = payload
    results["steps"].append("Sending update to HappyFox...")

    url = f"{base_url}/ticket/{ticket_id_param}/staff_update/"
    results["update_url"] = url
    resp = client.post(url, json=payload)
    results["status_code"] = resp.status_code
    results["response"] = resp.text[:2000]

    if resp.is_success:
        results["steps"].append(f"SUCCESS! Status: {resp.status_code}")
    else:
        results["steps"].append(f"FAILED: {resp.status_code} - {resp.text[:500]}")

    client.close()
