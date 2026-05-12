"""ADO-focused diagnostic actions: test_parent, ado_fields, test_mi_auth, ado_picklists, ado_projects."""
from __future__ import annotations

import json
import traceback
from typing import Any

import azure.functions as func

from health_check._helpers import error_response, json_response, run_diagnostic


# ---------------------------------------------------------------------------
# test_parent
# ---------------------------------------------------------------------------
@run_diagnostic
def test_parent(req: func.HttpRequest, results: dict[str, Any]) -> None:
    """Manually process one message from the parent queue and return results."""
    results["steps"].append("Importing servicebus_client...")
    from src.integration.services.servicebus_client import receive_from_parent_queue
    results["steps"].append("OK")

    results["steps"].append("Receiving from parent queue...")
    messages = receive_from_parent_queue(max_messages=1)
    results["message_count"] = len(messages)

    if not messages:
        results["steps"].append("No messages in queue")
        return

    raw_body = messages[0]
    results["raw_message"] = raw_body[:500]
    results["steps"].append("Got message, parsing event...")

    from src.integration.models.events import IntegrationEvent
    event = IntegrationEvent.model_validate_json(raw_body)
    results["event_type"] = event.event_type
    results["resource_id"] = event.resource_id
    results["steps"].append(f"Event parsed: type={event.event_type}, id={event.resource_id}")

    results["steps"].append("Importing AdoService...")
    from src.integration.services.ado_service import AdoService
    from src.integration.config import settings
    results["steps"].append("OK")

    results["steps"].append(f"Fetching work item {event.resource_id} from ADO...")
    ado = AdoService(settings)
    try:
        parent_id = int(event.resource_id)
        work_item = ado.get_work_item(parent_id)
        fields = work_item.get("fields", {})
        results["work_item_title"] = fields.get("System.Title", "")
        results["work_item_type"] = fields.get("System.WorkItemType", "")
        results["steps"].append(f"Got work item: {fields.get('System.Title', '')}")

        from src.integration.models.ado_models import AdoFieldNames
        clients_raw = fields.get(AdoFieldNames.CLIENT_REQUESTED, "")
        results["clients_raw"] = clients_raw
        if isinstance(clients_raw, str):
            clients = [c.strip() for c in clients_raw.split(";") if c.strip()]
        else:
            clients = []
        results["clients_parsed"] = clients
        results["steps"].append(f"Clients found: {clients}")

        if clients:
            results["steps"].append("Checking existing children...")
            from src.integration.functions.parent_story_processor import _get_existing_children_by_client
            existing = _get_existing_children_by_client(ado, parent_id)
            results["existing_children"] = {k: v for k, v in existing.items()}
            results["steps"].append(f"Existing children: {existing}")

            first_client = clients[0]
            if first_client.lower() not in existing:
                results["steps"].append(f"Attempting to create child for '{first_client}'...")
                try:
                    import httpx
                    from src.integration.models.ado_models import AdoWorkItemTypes

                    child_title = f"{first_client} - {fields.get('System.Title', '')}"
                    child_fields = {
                        AdoFieldNames.TITLE: child_title,
                        AdoFieldNames.CLIENT_REQUESTED: first_client,
                    }
                    for fld in [AdoFieldNames.DESCRIPTION, AdoFieldNames.ACCEPTANCE_CRITERIA,
                                AdoFieldNames.TEST_SCENARIOS, AdoFieldNames.PRIORITY,
                                AdoFieldNames.REQUEST_CATEGORY, AdoFieldNames.UAT_FEEDBACK_RESOLVED]:
                        val = fields.get(fld)
                        if val is not None:
                            child_fields[fld] = val

                    operations = [{"op": "add", "path": f"/fields/{k}", "value": v} for k, v in child_fields.items()]
                    operations.append({
                        "op": "add",
                        "path": "/relations/-",
                        "value": {
                            "rel": "System.LinkTypes.Hierarchy-Reverse",
                            "url": f"https://dev.azure.com/{settings.ado_organization}/{settings.ado_project}/_apis/wit/workitems/{parent_id}",
                            "attributes": {"comment": "Auto-created by integration"},
                        },
                    })

                    results["create_payload"] = operations
                    url = f"{settings.ado_base_url}/wit/workitems/${AdoWorkItemTypes.CLIENT_STORY}?api-version=7.1"
                    results["create_url"] = url

                    from azure.identity import DefaultAzureCredential as _DAC
                    _cred = _DAC()
                    _tok = _cred.get_token("499b84ac-1321-427f-aa17-267ca6975798/.default")
                    resp = httpx.post(
                        url,
                        json=operations,
                        headers={
                            "Authorization": f"Bearer {_tok.token}",
                            "Content-Type": "application/json-patch+json",
                        },
                        timeout=30.0,
                    )
                    results["ado_status"] = resp.status_code
                    results["ado_response"] = resp.text[:2000]

                    if resp.status_code < 300:
                        results["steps"].append(f"Child created: {resp.json().get('id')}")
                    else:
                        results["steps"].append(f"ADO returned {resp.status_code}: {resp.text[:500]}")

                except Exception as e:
                    results["child_create_error"] = str(e)
                    results["child_create_traceback"] = traceback.format_exc()
                    results["steps"].append(f"Child creation FAILED: {e}")
            else:
                results["steps"].append(f"Child already exists for '{first_client}': ID={existing[first_client.lower()]}")

    finally:
        ado.close()


# ---------------------------------------------------------------------------
# ado_fields
# ---------------------------------------------------------------------------
@run_diagnostic
def ado_fields(req: func.HttpRequest, results: dict[str, Any]) -> None:
    """Dump all field names and values from an ADO work item."""
    work_item_id = req.params.get("work_item_id", "26878")
    field_filter = req.params.get("filter", "").lower()
    results["work_item_id"] = work_item_id

    from src.integration.config import settings
    from src.integration.services.ado_service import AdoService

    ado = AdoService(settings)
    try:
        wi = ado.get_work_item(int(work_item_id))
        fields = wi.get("fields", {})

        if field_filter:
            filtered = {k: v for k, v in fields.items() if field_filter in k.lower()}
            results["filtered_fields"] = filtered
            results["steps"].append(f"Found {len(filtered)} fields matching '{field_filter}' (out of {len(fields)} total)")
        else:
            custom_fields = {k: v for k, v in fields.items() if k.startswith("Custom.")}
            results["custom_fields"] = custom_fields
            results["all_field_names"] = sorted(fields.keys())
            results["steps"].append(f"Found {len(custom_fields)} Custom.* fields (out of {len(fields)} total)")
    finally:
        ado.close()


# ---------------------------------------------------------------------------
# test_mi_auth
# ---------------------------------------------------------------------------
@run_diagnostic
def test_mi_auth(req: func.HttpRequest, results: dict[str, Any]) -> None:
    """Test Managed Identity authentication against ADO."""
    work_item_id = req.params.get("work_item_id", "26837")
    results["work_item_id"] = work_item_id

    from src.integration.config import settings
    results["ado_base_url"] = settings.ado_base_url
    results["ado_organization"] = settings.ado_organization
    results["ado_project"] = settings.ado_project

    results["steps"].append("Acquiring bearer token via DefaultAzureCredential...")
    import time
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    ado_resource = "499b84ac-1321-427f-aa17-267ca6975798"
    token_result = credential.get_token(f"{ado_resource}/.default")
    token = token_result.token
    expires_on = token_result.expires_on

    results["token_acquired"] = True
    results["token_length"] = len(token)
    results["token_prefix"] = token[:20] + "..."
    results["expires_on"] = expires_on
    results["expires_in_seconds"] = int(expires_on - time.time())
    results["steps"].append(f"Token acquired ({len(token)} chars), expires in {int(expires_on - time.time())}s")

    # Decode the JWT to inspect claims
    import base64
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            claims = json.loads(payload_bytes)
            safe_claims = {
                "aud": claims.get("aud"),
                "iss": claims.get("iss"),
                "oid": claims.get("oid"),
                "sub": claims.get("sub"),
                "appid": claims.get("appid"),
                "tid": claims.get("tid"),
                "roles": claims.get("roles"),
                "scp": claims.get("scp"),
            }
            results["token_claims"] = safe_claims
            results["steps"].append(f"Token oid={claims.get('oid')}, aud={claims.get('aud')}")
    except Exception as e:
        results["token_decode_error"] = str(e)
        results["steps"].append(f"Could not decode JWT: {e}")

    auth_header = f"Bearer {token}"

    import httpx
    url = f"{settings.ado_base_url}/wit/workitems/{work_item_id}"
    params = {"api-version": "7.1", "$expand": "all"}
    results["request_url"] = url
    results["steps"].append(f"GET {url}")

    client = httpx.Client(timeout=30.0)
    resp = client.get(url, params=params, headers={"Authorization": auth_header, "Content-Type": "application/json"})
    results["response_status"] = resp.status_code
    results["response_headers"] = dict(resp.headers)
    results["response_body"] = resp.text[:3000]
    results["steps"].append(f"Response: {resp.status_code}")

    if resp.is_success:
        data = resp.json()
        results["work_item_title"] = data.get("fields", {}).get("System.Title", "")
        results["steps"].append(f"SUCCESS — {data.get('fields', {}).get('System.Title', '')}")
    else:
        results["steps"].append(f"FAILED: {resp.status_code} — {resp.text[:500]}")

    results["steps"].append("Testing org-level access: listing projects...")
    projects_url = f"https://dev.azure.com/{settings.ado_organization}/_apis/projects"
    resp2 = client.get(projects_url, params={"api-version": "7.1", "$top": 5}, headers={"Authorization": auth_header})
    results["projects_status"] = resp2.status_code
    if resp2.is_success:
        projects = resp2.json().get("value", [])
        results["visible_projects"] = [p.get("name") for p in projects[:5]]
        results["steps"].append(f"Can see {len(projects)} projects: {[p.get('name') for p in projects[:5]]}")
    else:
        results["projects_response"] = resp2.text[:500]
        results["steps"].append(f"Projects endpoint: {resp2.status_code}")

    client.close()


# ---------------------------------------------------------------------------
# ado_picklists
# ---------------------------------------------------------------------------
@run_diagnostic
def ado_picklists(req: func.HttpRequest, results: dict[str, Any]) -> None:
    """Fetch ADO picklist items for a field via the Process API."""
    field_ref = req.params.get("field", "Custom.ClientRequested")
    wit_ref = req.params.get("wit", "")
    results["field"] = field_ref

    from src.integration.config import settings
    from src.integration.services.ado_service import AdoService
    import httpx

    ado = AdoService(settings)
    token = ado._get_bearer_token()
    org = settings.ado_organization
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    client = httpx.Client(timeout=30.0, headers=headers)

    # Get all processes
    results["steps"].append("Fetching org processes...")
    resp = client.get(f"https://dev.azure.com/{org}/_apis/work/processes", params={"api-version": "7.1-preview.2"})
    resp.raise_for_status()
    processes = resp.json().get("value", [])
    results["processes"] = [
        {"id": p["typeId"], "name": p["name"], "isDefault": p.get("isDefault", False)}
        for p in processes
    ]
    results["steps"].append(f"Found {len(processes)} processes")

    all_picklists: dict[str, Any] = {}
    for proc in processes:
        proc_id = proc["typeId"]
        proc_name = proc["name"]

        wit_url = f"https://dev.azure.com/{org}/_apis/work/processes/{proc_id}/workitemtypes"
        wit_resp = client.get(wit_url, params={"api-version": "7.1-preview.2"})
        if not wit_resp.is_success:
            continue

        wits = wit_resp.json().get("value", [])
        target_wits = [w for w in wits if w.get("name") == wit_ref or w.get("referenceName") == wit_ref] if wit_ref else wits

        for wit in target_wits:
            wit_ref_name = wit.get("referenceName", "")
            fields_url = f"https://dev.azure.com/{org}/_apis/work/processes/{proc_id}/workitemtypes/{wit_ref_name}/fields"
            fields_resp = client.get(fields_url, params={"api-version": "7.1-preview.2"})
            if not fields_resp.is_success:
                continue

            wit_fields = fields_resp.json().get("value", [])
            target_field = next((f for f in wit_fields if f.get("referenceName") == field_ref), None)

            if target_field:
                field_info: dict[str, Any] = {
                    "process": proc_name,
                    "process_id": proc_id,
                    "work_item_type": wit.get("name"),
                    "field_type": target_field.get("type", "unknown"),
                    "field_details": {k: v for k, v in target_field.items() if k != "url"},
                }

                picklist_id = target_field.get("picklistId")
                if picklist_id:
                    field_info["picklist_id"] = picklist_id
                    results["steps"].append(
                        f"Found field '{field_ref}' in process '{proc_name}', "
                        f"WIT '{wit.get('name')}' — type={target_field.get('type')}, picklistId={picklist_id}"
                    )

                    list_url = f"https://dev.azure.com/{org}/_apis/work/processes/lists/{picklist_id}"
                    list_resp = client.get(list_url, params={"api-version": "7.1-preview.1"})
                    if list_resp.is_success:
                        picklist_data = list_resp.json()
                        items = picklist_data.get("items", [])
                        results["steps"].append(f"Fetched picklist: {len(items)} items")
                        field_info["picklist_name"] = picklist_data.get("name", "")
                        field_info["picklist_type"] = picklist_data.get("type", "")
                        field_info["items"] = items
                        field_info["item_count"] = len(items)
                        mapping_template = {}
                        for item in items:
                            key = item.get("id", item.get("value", "")) if isinstance(item, dict) else item
                            mapping_template[key] = "<HF_CHOICE_ID>"
                        field_info["mapping_template"] = mapping_template
                    else:
                        results["steps"].append(f"Failed to fetch picklist {picklist_id}: {list_resp.status_code}")
                else:
                    results["steps"].append(
                        f"Found field '{field_ref}' in process '{proc_name}', "
                        f"WIT '{wit.get('name')}' — type={target_field.get('type')}, NO picklistId."
                    )

                # Try allowed values from project-scoped API
                try:
                    av_url = f"https://dev.azure.com/{org}/_apis/wit/fields/{field_ref}"
                    av_resp = client.get(av_url, params={"api-version": "7.1", "$expand": "allowedValues"})
                    if av_resp.is_success:
                        av_data = av_resp.json()
                        field_info["field_api_info"] = {
                            "name": av_data.get("name"),
                            "referenceName": av_data.get("referenceName"),
                            "type": av_data.get("type"),
                            "isPicklist": av_data.get("isPicklist"),
                            "isPicklistSuggested": av_data.get("isPicklistSuggested"),
                            "picklistId": av_data.get("picklistId"),
                            "isIdentity": av_data.get("isIdentity"),
                        }
                except Exception:
                    pass

                all_picklists[f"{proc_name}/{wit.get('name')}"] = field_info
                break

        if all_picklists:
            break

    results["picklists"] = all_picklists
    if not all_picklists:
        results["steps"].append(
            f"Could not find picklist for field '{field_ref}' in any process/WIT. "
            "Try specifying &wit=<WorkItemType> or check the field reference name."
        )

    client.close()
    ado.close()


# ---------------------------------------------------------------------------
# ado_projects
# ---------------------------------------------------------------------------
@run_diagnostic
def ado_projects(req: func.HttpRequest, results: dict[str, Any]) -> None:
    """List all ADO projects with their GUIDs for MAPPING_PROJECT_TO_PRODUCT."""
    from src.integration.config import settings
    from src.integration.services.ado_service import AdoService
    import httpx

    results["steps"].append("Acquiring ADO credentials...")
    ado = AdoService(settings)
    token = ado._get_bearer_token()

    org = settings.ado_organization
    url = f"https://dev.azure.com/{org}/_apis/projects"
    params = {"api-version": "7.1", "$top": 100}

    results["steps"].append(f"GET {url}")
    client = httpx.Client(timeout=30.0)
    resp = client.get(url, params=params, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    client.close()

    results["response_status"] = resp.status_code
    if resp.is_success:
        projects = resp.json().get("value", [])
        results["project_count"] = len(projects)
        project_list = []
        mapping_example = {}
        for p in projects:
            pid = p.get("id", "")
            name = p.get("name", "")
            project_list.append({"id": pid, "name": name, "state": p.get("state", "")})
            mapping_example[pid] = f"<HF_PRODUCT_ID_for_{name}>"
        results["projects"] = project_list
        results["mapping_template"] = mapping_example
        results["steps"].append(
            f"Found {len(projects)} projects. Copy project IDs into MAPPING_PROJECT_TO_PRODUCT app setting."
        )
    else:
        results["error"] = resp.text[:1000]
        results["steps"].append(f"FAILED: {resp.status_code}")
