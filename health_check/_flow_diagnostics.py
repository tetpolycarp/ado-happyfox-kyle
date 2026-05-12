"""End-to-end flow diagnostic actions: test_child_flow, test_attachments, test_comments."""
from __future__ import annotations

import traceback
from typing import Any

import azure.functions as func

from health_check._helpers import error_response, json_response, run_diagnostic


# ---------------------------------------------------------------------------
# test_child_flow
# ---------------------------------------------------------------------------
def test_child_flow(req: func.HttpRequest) -> func.HttpResponse:
    """Manually run the child processor logic for a specific child work item."""
    child_id_param = req.params.get("child_id", "")
    if not child_id_param:
        return error_response("child_id required. Usage: ?action=test_child_flow&child_id=26856")

    results: dict[str, Any] = {"steps": [], "child_id": child_id_param}
    try:
        _run_test_child_flow(req, results, child_id_param)
    except Exception as e:
        results["error"] = str(e)
        results["traceback"] = traceback.format_exc()
        results["steps"].append(f"FAILED: {e}")

    return json_response(results)


def _run_test_child_flow(req: func.HttpRequest, results: dict[str, Any], child_id_param: str) -> None:
    from src.integration.config import settings
    from src.integration.services.ado_service import AdoService
    from src.integration.services.happyfox_service import HappyFoxService
    from src.integration.services.transform_service import (
        ado_client_story_to_happyfox_create,
        ado_client_story_to_happyfox_update,
    )
    from src.integration.models.ado_models import AdoFieldNames

    ado = AdoService(settings)
    hf = HappyFoxService(settings)
    try:
        child_id = int(child_id_param)

        # Step 1: Fetch the child work item
        results["steps"].append(f"Fetching ADO work item {child_id}...")
        work_item = ado.get_work_item(child_id)
        fields = work_item.get("fields", {})
        results["title"] = fields.get("System.Title", "")
        results["work_item_type"] = fields.get("System.WorkItemType", "")
        results["steps"].append(f"Got: {fields.get('System.Title', '')}")

        # Step 2: Resolve parent
        parent_id = fields.get(AdoFieldNames.ADO_PARENT_ID, "")
        if not parent_id:
            resolved = ado.get_parent_id(child_id)
            parent_id = str(resolved) if resolved else ""
        results["parent_id"] = parent_id
        results["steps"].append(f"Parent ID: {parent_id}")

        # Step 3: Check for existing HF ticket
        support_ticket_number = fields.get(AdoFieldNames.SUPPORT_TICKET_NUMBER, "")
        results["support_ticket_number"] = support_ticket_number
        results["steps"].append(f"Support Ticket Number in ADO: '{support_ticket_number}'")

        hf_ticket_id = None
        if support_ticket_number:
            try:
                hf_id = int(support_ticket_number)
                ticket = hf.get_ticket(hf_id)
                if ticket is not None:
                    hf_ticket_id = ticket.id
                    results["steps"].append(f"Found existing HF ticket by Support Ticket Number: {hf_ticket_id}")
            except (ValueError, TypeError) as e:
                results["steps"].append(f"Invalid Support Ticket Number: {e}")

        if hf_ticket_id is None:
            results["steps"].append("Searching HF by DEV Ticket Number...")
            ticket = hf.find_ticket_by_ado_child_id(child_id_param)
            if ticket is not None:
                hf_ticket_id = ticket.id
                results["steps"].append(f"Found existing HF ticket by search: {hf_ticket_id}")
            else:
                results["steps"].append("No existing HF ticket found")

        results["hf_ticket_id"] = hf_ticket_id

        if hf_ticket_id is not None:
            _handle_update_path(req, results, fields, hf, hf_ticket_id)
        else:
            _handle_create_path(req, results, fields, ado, hf, settings, child_id, child_id_param, parent_id)

    finally:
        ado.close()
        hf.close()


def _handle_update_path(
    req: func.HttpRequest,
    results: dict[str, Any],
    fields: dict,
    hf,
    hf_ticket_id: int,
) -> None:
    """Handle the update branch of test_child_flow."""
    from src.integration.services.transform_service import ado_client_story_to_happyfox_update

    results["steps"].append("Building update payload...")
    update_payload = ado_client_story_to_happyfox_update(fields=fields, hf_service=hf)
    results["update_payload_text_length"] = len(update_payload.text or "")
    results["update_payload_priority"] = update_payload.priority
    results["update_payload_subject"] = update_payload.subject
    results["update_payload_custom_fields"] = update_payload.custom_fields

    new_text = update_payload.text or ""
    description_changed = False
    if new_text:
        existing_html = hf.get_latest_description_html(hf_ticket_id)
        results["existing_description_length"] = len(existing_html)
        results["description_match"] = new_text.strip() == existing_html.strip()
        if new_text.strip() != existing_html.strip():
            description_changed = True
        else:
            update_payload.text = None

    has_priority = update_payload.priority is not None
    has_custom_fields = bool(update_payload.custom_fields)
    has_subject = update_payload.subject is not None
    would_update = description_changed or has_priority or has_custom_fields or has_subject

    results["description_changed"] = description_changed
    results["has_priority"] = has_priority
    results["has_custom_fields"] = has_custom_fields
    results["has_subject"] = has_subject
    results["would_send_update"] = would_update
    results["steps"].append(
        f"Change detection: desc_changed={description_changed}, "
        f"priority={has_priority}, custom_fields={has_custom_fields}, "
        f"subject={has_subject}, would_update={would_update}"
    )

    if req.params.get("execute") == "true":
        if would_update:
            results["steps"].append("Sending update to HappyFox...")
            hf.update_ticket(hf_ticket_id, update_payload)
            results["steps"].append("Update sent successfully!")
        else:
            results["steps"].append("Skipped — no changes to send")
    else:
        results["steps"].append("DRY RUN — add &execute=true to actually send the update")


def _handle_create_path(
    req: func.HttpRequest,
    results: dict[str, Any],
    fields: dict,
    ado,
    hf,
    settings,
    child_id: int,
    child_id_param: str,
    parent_id: str,
) -> None:
    """Handle the create branch of test_child_flow."""
    from src.integration.services.transform_service import ado_client_story_to_happyfox_create
    from src.integration.models.ado_models import AdoFieldNames

    if req.params.get("execute") != "true":
        results["steps"].append("Would create new HF ticket — add &execute=true to run")
        return

    results["steps"].append("Creating new HF ticket...")

    # Resolve parent title
    parent_title = ""
    if parent_id:
        try:
            parent_wi = ado.get_work_item(int(parent_id))
            parent_title = parent_wi.get("fields", {}).get("System.Title", "")
        except Exception as e:
            results["steps"].append(f"Could not fetch parent title: {e}")

    create_payload = ado_client_story_to_happyfox_create(
        fields=fields,
        hf_service=hf,
        category_id=settings.hf_default_category_id,
        ado_parent_title=parent_title,
        ado_child_id=child_id_param,
    )
    create_payload.email = settings.hf_create_user_email
    create_payload.name = settings.hf_create_user_name

    api_payload = create_payload.to_api_payload()
    results["create_api_payload"] = api_payload

    import httpx
    base_url = settings.happyfox_api_url.rstrip("/")
    auth = (settings.happyfox_api_key.get_secret_value(), settings.happyfox_auth_code.get_secret_value())
    hf_raw = httpx.Client(auth=auth, timeout=30.0)
    resp = hf_raw.post(f"{base_url}/tickets/", json=api_payload)
    results["hf_status_code"] = resp.status_code
    results["hf_response_body"] = resp.text[:2000]

    if resp.is_success:
        new_ticket_data = resp.json()
        new_hf_id = new_ticket_data.get("id")
        results["new_hf_ticket_id"] = new_hf_id
        results["steps"].append(f"Created HF ticket {new_hf_id}")

        # Write HF ticket ID back to ADO
        ado.update_work_item(child_id, {
            AdoFieldNames.SUPPORT_TICKET_NUMBER: str(new_hf_id),
        })
        results["steps"].append(f"Wrote Support Ticket Number {new_hf_id} back to ADO")
    else:
        results["steps"].append(f"HF create failed: {resp.status_code} — {resp.text[:500]}")

    hf_raw.close()


# ---------------------------------------------------------------------------
# test_attachments
# ---------------------------------------------------------------------------
def test_attachments(req: func.HttpRequest) -> func.HttpResponse:
    """Test attachment retrieval from ADO and optional upload to HappyFox."""
    child_id_param = req.params.get("child_id", "")
    if not child_id_param:
        return error_response("child_id required. Usage: ?action=test_attachments&child_id=12345&ticket_id=666")

    ticket_id_param = req.params.get("ticket_id", "")
    results: dict[str, Any] = {"steps": [], "child_id": child_id_param}
    try:
        _run_test_attachments(results, child_id_param, ticket_id_param)
    except Exception as e:
        results["error"] = str(e)
        results["traceback"] = traceback.format_exc()
        results["steps"].append(f"FAILED: {e}")

    return json_response(results)


def _run_test_attachments(results: dict[str, Any], child_id_param: str, ticket_id_param: str) -> None:
    from src.integration.config import settings
    from src.integration.services.ado_service import AdoService
    from src.integration.services.happyfox_service import HappyFoxService

    ado = AdoService(settings)
    hf = HappyFoxService(settings)
    try:
        child_id = int(child_id_param)

        results["steps"].append(f"Fetching work item {child_id} with relations...")
        work_item = ado.get_work_item(child_id, expand="relations")
        relations = work_item.get("relations", []) or []
        results["total_relations"] = len(relations)
        results["relation_types"] = list(set(r.get("rel", "?") for r in relations))

        attached_files = _extract_attached_files(relations)
        results["attached_files"] = attached_files
        results["attachment_count"] = len(attached_files)
        results["steps"].append(f"Found {len(attached_files)} AttachedFile relations")

        if not attached_files:
            results["steps"].append("No attachments on child — checking parent work item...")
            parent_id = ado.get_parent_id(child_id)
            results["parent_id"] = parent_id
            if parent_id:
                parent_wi = ado.get_work_item(parent_id, expand="relations")
                parent_relations = parent_wi.get("relations", []) or []
                attached_files = _extract_attached_files(parent_relations, source="parent")
                results["parent_attachment_count"] = len(attached_files)
                results["attached_files"] = attached_files
                results["steps"].append(f"Found {len(attached_files)} attachments on parent {parent_id}")
            else:
                results["steps"].append("Could not resolve parent ID")

        if not attached_files:
            results["steps"].append("NO ATTACHMENTS found on child or parent.")
            return

        # Download the first attachment
        first = attached_files[0]
        results["steps"].append(f"Downloading first attachment: {first['name']} ({first['resource_size']} bytes)...")
        file_bytes = ado.download_attachment(first["url"])
        results["downloaded_size"] = len(file_bytes)
        results["steps"].append(f"Downloaded {len(file_bytes)} bytes")

        # If ticket_id provided, test upload to HappyFox
        if ticket_id_param:
            _test_hf_attachment_upload(results, hf, settings, int(ticket_id_param), first, file_bytes)

    finally:
        ado.close()
        hf.close()


def _extract_attached_files(relations: list, source: str = "child") -> list[dict]:
    """Extract AttachedFile entries from ADO work item relations."""
    files = []
    for r in relations:
        if r.get("rel") == "AttachedFile":
            entry: dict[str, Any] = {
                "rel": r.get("rel"),
                "url": r.get("url", ""),
                "name": r.get("attributes", {}).get("name", "unknown"),
                "resource_size": r.get("attributes", {}).get("resourceSize", 0),
            }
            if source != "child":
                entry["source"] = source
            files.append(entry)
    return files


def _test_hf_attachment_upload(
    results: dict[str, Any], hf, settings, hf_ticket_id: int, attachment: dict, file_bytes: bytes
) -> None:
    """Test uploading an attachment to a HappyFox ticket."""
    import httpx

    results["steps"].append(f"Checking existing attachments on HF ticket {hf_ticket_id}...")
    existing_names = hf.get_ticket_attachment_names(hf_ticket_id)
    results["existing_hf_attachments"] = list(existing_names)

    # Dump raw update structures for debug
    base = settings.happyfox_api_url.rstrip("/")
    auth = (settings.happyfox_api_key.get_secret_value(), settings.happyfox_auth_code.get_secret_value())
    hf_client = httpx.Client(auth=auth, timeout=30.0)
    tresp = hf_client.get(f"{base}/ticket/{hf_ticket_id}/")
    if tresp.is_success:
        tdata = tresp.json()
        upd_debug = []
        for upd in tdata.get("updates", []):
            entry: dict[str, Any] = {"update_id": upd.get("id"), "keys": list(upd.keys())}
            for k, v in upd.items():
                if "attach" in k.lower() or "file" in k.lower():
                    entry[k] = v
            if "message" in upd:
                msg = upd["message"]
                if isinstance(msg, dict):
                    entry["message_keys"] = list(msg.keys())
                    for mk, mv in msg.items():
                        if "attach" in mk.lower() or "file" in mk.lower():
                            entry[f"message.{mk}"] = mv
            upd_debug.append(entry)
        results["hf_update_debug"] = upd_debug
    hf_client.close()

    if attachment["name"] in existing_names:
        results["steps"].append(f"Attachment '{attachment['name']}' already exists on HF ticket — would skip in normal flow")
    else:
        results["steps"].append(f"Uploading '{attachment['name']}' to HF ticket {hf_ticket_id}...")
        upload_result = hf.add_attachment(hf_ticket_id, attachment["name"], file_bytes)
        results["upload_response"] = str(upload_result)[:2000]
        results["steps"].append("Upload SUCCESS!")


# ---------------------------------------------------------------------------
# test_comments
# ---------------------------------------------------------------------------
def test_comments(req: func.HttpRequest) -> func.HttpResponse:
    """Test ADO comment fetching and HappyFox private note posting."""
    work_item_id = req.params.get("work_item_id", "26878")
    hf_ticket_id = req.params.get("hf_ticket_id", "")
    do_post = req.params.get("post", "").lower() == "true"

    results: dict[str, Any] = {"steps": [], "work_item_id": work_item_id}
    try:
        _run_test_comments(req, results, work_item_id, hf_ticket_id, do_post)
    except Exception as e:
        results["error"] = str(e)
        results["traceback"] = traceback.format_exc()
        results["steps"].append(f"FAILED: {e}")

    return json_response(results)


def _run_test_comments(
    req: func.HttpRequest,
    results: dict[str, Any],
    work_item_id: str,
    hf_ticket_id: str,
    do_post: bool,
) -> None:
    from src.integration.config import settings
    from src.integration.services.ado_service import AdoService
    from src.integration.services.happyfox_service import HappyFoxService

    ado = AdoService(settings)
    try:
        comments = ado.get_comments(int(work_item_id))
        results["ado_comments"] = [
            {
                "id": c.get("id"),
                "text_preview": (c.get("text", "") or "")[:200],
                "createdBy": c.get("createdBy", {}).get("displayName", "?"),
                "createdDate": c.get("createdDate", ""),
            }
            for c in comments
        ]
        results["steps"].append(f"Fetched {len(comments)} comments from ADO work item {work_item_id}")

        parent_id = ado.get_parent_id(int(work_item_id))
        if parent_id:
            parent_comments = ado.get_comments(parent_id)
            results["parent_id"] = parent_id
            results["parent_comments"] = [
                {
                    "id": c.get("id"),
                    "text_preview": (c.get("text", "") or "")[:200],
                    "createdBy": c.get("createdBy", {}).get("displayName", "?"),
                    "createdDate": c.get("createdDate", ""),
                }
                for c in parent_comments
            ]
            results["steps"].append(f"Fetched {len(parent_comments)} comments from parent {parent_id}")
        else:
            results["steps"].append("No parent found for this work item")
    finally:
        ado.close()

    if not hf_ticket_id:
        results["steps"].append("Add &hf_ticket_id=X to test HF private note posting")
        return

    hf = HappyFoxService(settings)
    try:
        synced_ids = hf.get_synced_comment_ids(int(hf_ticket_id))
        results["hf_synced_comment_ids"] = sorted(synced_ids)
        results["steps"].append(f"Found {len(synced_ids)} already-synced comment IDs in HF ticket {hf_ticket_id}")

        # Dump private note updates for debug
        try:
            raw_url = f"{hf._base_url}/ticket/{int(hf_ticket_id)}/"
            raw_resp = hf._client.get(raw_url)
            raw_resp.raise_for_status()
            raw_data = raw_resp.json()
            pvt_updates = []
            for update in raw_data.get("updates", []):
                msg = update.get("message")
                if isinstance(msg, dict) and msg.get("message_type") == "p":
                    pvt_updates.append({
                        "message_type": msg.get("message_type"),
                        "html_preview": (msg.get("html", "") or "")[:200],
                        "text_preview": (msg.get("text", "") or "")[:200],
                        "all_msg_keys": list(msg.keys()),
                    })
            results["private_note_updates"] = pvt_updates
            results["steps"].append(f"Found {len(pvt_updates)} private note updates in HF ticket")
        except Exception as e:
            results["steps"].append(f"Failed to dump updates: {e}")

        if do_post:
            _test_private_note_endpoints(results, settings, int(hf_ticket_id))
        else:
            results["steps"].append("Add &post=true to test posting a private note")
    finally:
        hf.close()


def _test_private_note_endpoints(results: dict[str, Any], settings, tid: int) -> None:
    """Try multiple private note endpoint/param combinations."""
    import httpx

    hf_base = settings.happyfox_api_url.rstrip("/")
    hf_auth = (settings.happyfox_api_key.get_secret_value(), settings.happyfox_auth_code.get_secret_value())

    staff_id = None
    try:
        staff_resp = httpx.get(f"{hf_base}/staff/", auth=hf_auth, timeout=15)
        for s in staff_resp.json():
            if "kyle.bring" in s.get("email", "").lower():
                staff_id = s["id"]
                break
    except Exception:
        pass

    attempts = [
        ("staff_pvtnote", f"{hf_base}/ticket/{tid}/staff_pvtnote/", {"html": "<p>Test pvtnote endpoint</p>"}),
        ("private-note", f"{hf_base}/ticket/{tid}/private-note/", {"html": "<p>Test private-note endpoint</p>"}),
        ("private_note", f"{hf_base}/ticket/{tid}/private_note/", {"html": "<p>Test private_note endpoint</p>"}),
        ("staff_update+update_type", f"{hf_base}/ticket/{tid}/staff_update/", {"html": "<p>Test update_type=private</p>", "update_type": "private"}),
        ("staff_update+private=true", f"{hf_base}/ticket/{tid}/staff_update/", {"html": "<p>Test private=true</p>", "private": True}),
        ("staff_update+is_private=true", f"{hf_base}/ticket/{tid}/staff_update/", {"html": "<p>Test is_private=true</p>", "is_private": True}),
        ("staff_update+visibility=private", f"{hf_base}/ticket/{tid}/staff_update/", {"html": "<p>Test visibility=private</p>", "visibility": "private"}),
    ]
    results["endpoint_tests"] = []
    for name, url, payload in attempts:
        if staff_id:
            payload["staff"] = staff_id
        try:
            r = httpx.post(url, json=payload, auth=hf_auth, timeout=15)
            msg_type = None
            if r.is_success:
                rj = r.json()
                updates = rj.get("updates", [])
                if updates:
                    last_msg = updates[-1].get("message", {})
                    if isinstance(last_msg, dict):
                        msg_type = last_msg.get("message_type")
            results["endpoint_tests"].append({"name": name, "status": r.status_code, "message_type": msg_type, "ok": r.is_success})
            results["steps"].append(f"{name}: {r.status_code} — message_type={msg_type}")
        except Exception as e:
            results["endpoint_tests"].append({"name": name, "error": str(e)})
            results["steps"].append(f"{name}: ERROR — {e}")
