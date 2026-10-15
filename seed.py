"""Provision demo content, collaborators, File Request, and upload webhook."""

from __future__ import annotations

from ipaddress import ip_address
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from box_sdk_gen import (
    BoxClient,
    BoxDeveloperTokenAuth,
    CreateCollaborationAccessibleBy,
    CreateCollaborationAccessibleByTypeField,
    CreateCollaborationItem,
    CreateCollaborationItemTypeField,
    CreateCollaborationRole,
    CreateFolderParent,
    CreateFileRequestCopyFolder,
    CreateFileRequestCopyFolderTypeField,
    CreateFileRequestCopyStatus,
    CreateMetadataTemplateFields,
    CreateMetadataTemplateFieldsTypeField,
    CreateWebhookTarget,
    CreateWebhookTargetTypeField,
    CreateWebhookTriggers,
    UpdateFileRequestByIdStatus,
    UpdateMetadataTemplateRequestBody,
    UpdateMetadataTemplateRequestBodyOpField,
    UpdateMetadataTemplateScope,
    UpdateWebhookByIdTriggers,
    UploadFileAttributes,
    UploadFileAttributesParentField,
)
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
SAMPLES_DIR = BASE_DIR / "sample_documents"
STATE_PATH = BASE_DIR / ".demo_state.json"
METADATA_TEMPLATE_SCOPE = "enterprise"
METADATA_TEMPLATE_KEY = "coiIntakeReview"
METADATA_TEMPLATE_NAME = "COI Intake Review"

METADATA_FIELDS = [
    ("requirements_project_completion_date", "Project completion date", "string"),
    ("requirements_general_liability_minimum", "Required general liability", "string"),
    ("requirements_auto_liability_minimum", "Required auto liability", "string"),
    ("requirements_workers_comp_required", "Workers compensation requirement", "string"),
    ("requirements_additional_insured_requirement", "Additional insured requirement", "string"),
    ("coi_named_insured", "Named insured", "string"),
    ("coi_policy_expiration_date", "Policy expiration date", "string"),
    ("coi_general_liability_per_occurrence_limit", "General liability per occurrence", "string"),
    ("coi_auto_liability_limit", "Auto liability limit", "string"),
    ("coi_workers_comp_evidence", "Workers compensation evidence", "string"),
    ("coi_additional_insured_statement", "Additional insured statement", "string"),
    ("coi_endorsement_evidence", "Separate endorsement evidence", "string"),
    ("decision_route", "Decisions route", "string"),
    ("decision_confidence", "Decisions confidence", "float"),
    ("decision_note", "Decisions note", "string"),
]


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} in .env before running the seed script.")
    return value


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"processed_file_ids": [], "collaborated_user_ids": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def box_status(error: Exception) -> int | None:
    response_info = getattr(error, "response_info", None)
    return getattr(response_info, "status_code", None) or getattr(error, "status_code", None)


def metadata_template_fields() -> list[CreateMetadataTemplateFields]:
    field_types = {
        "string": CreateMetadataTemplateFieldsTypeField.STRING,
        "float": CreateMetadataTemplateFieldsTypeField.FLOAT,
    }
    return [
        CreateMetadataTemplateFields(
            type=field_types[field_type],
            key=key,
            display_name=display_name,
            description=f"Extracted or selected by the COI intake demo: {display_name}.",
            hidden=False,
        )
        for key, display_name, field_type in METADATA_FIELDS
    ]


def ensure_metadata_template(client: BoxClient, state: dict) -> None:
    try:
        template = client.metadata_templates.get_metadata_template(
            METADATA_TEMPLATE_SCOPE,
            METADATA_TEMPLATE_KEY,
        )
        action = "reused"
    except Exception as error:
        if box_status(error) != 404:
            if box_status(error) == 403:
                raise RuntimeError(
                    "Box denied access to the enterprise metadata template. The app and authenticated "
                    "Box user need permission to create and read enterprise metadata templates; "
                    "see the README prerequisites."
                ) from error
            raise
        try:
            template = client.metadata_templates.create_metadata_template(
                METADATA_TEMPLATE_SCOPE,
                METADATA_TEMPLATE_NAME,
                template_key=METADATA_TEMPLATE_KEY,
                hidden=False,
                fields=metadata_template_fields(),
                copy_instance_on_item_copy=False,
            )
            action = "created"
        except Exception as create_error:
            if box_status(create_error) == 403:
                raise RuntimeError(
                    "Box denied creation of the enterprise metadata template. The app and authenticated "
                    "Box user need permission to create enterprise metadata templates; "
                    "see the README prerequisites."
                ) from create_error
            if box_status(create_error) == 409:
                template = client.metadata_templates.get_metadata_template(
                    METADATA_TEMPLATE_SCOPE,
                    METADATA_TEMPLATE_KEY,
                )
                action = "reused"
            else:
                raise

    expected_fields = {
        key: (display_name, field_type)
        for key, display_name, field_type in METADATA_FIELDS
    }
    existing_name = getattr(template, "display_name", None)
    if existing_name and existing_name != METADATA_TEMPLATE_NAME:
        raise RuntimeError(
            f"Box template key {METADATA_TEMPLATE_KEY!r} belongs to {existing_name!r}, "
            f"expected {METADATA_TEMPLATE_NAME!r}. Choose a different metadata template key."
        )
    existing_fields = {
        field.key: field
        for field in (getattr(template, "fields", None) or [])
        if getattr(field, "key", None)
    }
    incompatible = []
    additions = []
    for key, (display_name, field_type) in expected_fields.items():
        field = existing_fields.get(key)
        if field is None:
            additions.append((key, display_name, field_type))
            continue
        actual_field_type = getattr(field, "type", "")
        actual_type = str(getattr(actual_field_type, "value", actual_field_type))
        if actual_type != field_type:
            incompatible.append(f"{key} is {actual_type or 'unknown'}, expected {field_type}")
    if incompatible:
        raise RuntimeError(
            f"Existing Box metadata template {METADATA_TEMPLATE_KEY!r} has incompatible fields: "
            + "; ".join(incompatible)
        )
    if additions:
        try:
            client.metadata_templates.update_metadata_template(
                UpdateMetadataTemplateScope.ENTERPRISE,
                METADATA_TEMPLATE_KEY,
                [
                    UpdateMetadataTemplateRequestBody(
                        op=UpdateMetadataTemplateRequestBodyOpField.ADDFIELD,
                        field_key=key,
                        data={
                            "type": field_type,
                            "displayName": display_name,
                            "description": (
                                "Extracted or selected by the COI intake demo: "
                                f"{display_name}."
                            ),
                            "hidden": False,
                        },
                    )
                    for key, display_name, field_type in additions
                ],
            )
        except Exception as error:
            if box_status(error) == 403:
                raise RuntimeError(
                    "Box denied adding fields to the enterprise metadata template. The app and "
                    "authenticated Box user need permission to edit enterprise metadata templates."
                ) from error
            raise
        action = "updated"

    state["metadata_template_scope"] = METADATA_TEMPLATE_SCOPE
    state["metadata_template_key"] = METADATA_TEMPLATE_KEY
    state["metadata_template_id"] = getattr(template, "id", None)
    save_state(state)
    print(
        f"Enterprise metadata template {action}: {METADATA_TEMPLATE_NAME} "
        f"({METADATA_TEMPLATE_KEY}, {len(METADATA_FIELDS)} fields)."
    )


def list_items(client: BoxClient, folder_id: str) -> list:
    return client.folders.get_folder_items(
        folder_id,
        fields=["id", "name", "type"],
        limit=100,
    ).entries or []


def ensure_folder(client: BoxClient, parent_id: str, name: str) -> str:
    for item in list_items(client, parent_id):
        if item.type == "folder" and item.name == name:
            return item.id
    return client.folders.create_folder(name, CreateFolderParent(id=parent_id)).id


def ensure_upload(client: BoxClient, folder_id: str, path: Path) -> str:
    for item in list_items(client, folder_id):
        if item.type == "file" and item.name == path.name:
            return item.id
    attributes = UploadFileAttributes(
        name=path.name,
        parent=UploadFileAttributesParentField(id=folder_id),
    )
    with path.open("rb") as content:
        uploaded = client.uploads.upload_file(
            attributes,
            content,
            file_file_name=path.name,
            file_content_type="application/pdf",
        )
    return uploaded.entries[0].id


def ensure_file_request(client: BoxClient, folder_id: str, template_id: str, state: dict):
    options = {
        "title": "Submit project insurance certificate",
        "description": "Upload the certificate and any separate additional-insured endorsement for this project.",
        "is_email_required": False,
        "is_description_required": False,
    }
    existing_id = state.get("file_request_id")
    if existing_id:
        file_request = client.file_requests.update_file_request_by_id(
            existing_id,
            **options,
            status=UpdateFileRequestByIdStatus.ACTIVE,
        )
    else:
        if not template_id.isdecimal():
            raise RuntimeError(
                "BOX_FILE_REQUEST_TEMPLATE_ID must be the numeric ID from the Box File Request builder URL "
                "(`/filerequest/<id>`), not the public upload-link token (`/f/<token>`)."
            )
        try:
            template = client.file_requests.get_file_request_by_id(template_id)
        except Exception as error:
            if box_status(error) == 404:
                raise RuntimeError(
                    f"Box could not find File Request {template_id}. Use the ID from the File Request "
                    "builder URL (`/filerequest/<id>`), and make sure the token owner can access it. "
                    "The public upload link uses a different `/f/<token>` value."
                ) from error
            raise
        if str(getattr(getattr(template, "folder", None), "id", "")) == str(folder_id):
            file_request = client.file_requests.update_file_request_by_id(
                template_id,
                **options,
                status=UpdateFileRequestByIdStatus.ACTIVE,
            )
        else:
            file_request = client.file_requests.create_file_request_copy(
                template_id,
                CreateFileRequestCopyFolder(
                    id=folder_id,
                    type=CreateFileRequestCopyFolderTypeField.FOLDER,
                ),
                **options,
                status=CreateFileRequestCopyStatus.ACTIVE,
            )
    state["file_request_id"] = file_request.id
    state["file_request_url"] = file_request.url
    save_state(state)
    return file_request


def public_file_request_url(url: str) -> str:
    if url.startswith(("https://", "http://")):
        return url
    return f"https://app.box.com/{url.lstrip('/')}"


def add_reviewer(client: BoxClient, folder_id: str, user_id: str, state: dict) -> None:
    if user_id in state["collaborated_user_ids"]:
        return
    try:
        client.user_collaborations.create_collaboration(
            CreateCollaborationItem(
                id=folder_id,
                type=CreateCollaborationItemTypeField.FOLDER,
            ),
            CreateCollaborationAccessibleBy(
                id=user_id,
                type=CreateCollaborationAccessibleByTypeField.USER,
            ),
            CreateCollaborationRole.VIEWER,
            notify=False,
        )
    except Exception as error:
        # A previous run may have added this collaborator before state was saved.
        if not any(token in str(error).lower() for token in ("409", "conflict", "already")):
            raise
        print(f"Reviewer {user_id} already has access to the demo folder.")
    state["collaborated_user_ids"].append(user_id)
    save_state(state)


def validate_webhook_address(address: str) -> None:
    if "<" in address or ">" in address:
        raise RuntimeError(
            "Replace the BOX_WEBHOOK_ADDRESS placeholder with the actual public HTTPS tunnel URL."
        )
    try:
        parsed = urlsplit(address)
        port = parsed.port
    except ValueError as error:
        raise RuntimeError(f"Invalid BOX_WEBHOOK_ADDRESS: {error}") from error

    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(
            "BOX_WEBHOOK_ADDRESS must be a public HTTPS URL ending in /box-webhook. "
            "Use the public URL from your HTTPS tunnel, not localhost."
        )
    if port not in (None, 443):
        raise RuntimeError(
            "Box V2 webhooks require public HTTPS on port 443. Run `ngrok http 8000` "
            "and use the public hostname it gives you, without :8000."
        )
    if parsed.path != "/box-webhook" or parsed.query or parsed.fragment:
        raise RuntimeError(
            "BOX_WEBHOOK_ADDRESS must end exactly with /box-webhook and have no query or fragment."
        )

    hostname = parsed.hostname.lower()
    try:
        ip = ip_address(hostname)
    except ValueError:
        if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
            raise RuntimeError(
                "Box cannot reach localhost. Run `ngrok http 8000`, then use its public "
                "HTTPS hostname with /box-webhook."
            )
    else:
        if not ip.is_global:
            raise RuntimeError(
                "BOX_WEBHOOK_ADDRESS must use a public IP address; Box cannot reach localhost or private IPs."
            )


def ensure_upload_webhook(client: BoxClient, folder_id: str, address: str) -> str:
    entries = client.webhooks.get_webhooks().entries or []
    existing = next(
        (
            webhook for webhook in entries
            if str(getattr(getattr(webhook, "target", None), "id", "")) == str(folder_id)
        ),
        None,
    )
    if existing:
        webhook = client.webhooks.update_webhook_by_id(
            existing.id,
            address=address,
            triggers=[UpdateWebhookByIdTriggers.FILE_UPLOADED],
        )
    else:
        webhook = client.webhooks.create_webhook(
            CreateWebhookTarget(
                id=folder_id,
                type=CreateWebhookTargetTypeField.FOLDER,
            ),
            address,
            [CreateWebhookTriggers.FILE_UPLOADED],
        )
    if not webhook.id:
        raise RuntimeError("Box created a webhook without returning an ID.")
    return webhook.id


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    client = BoxClient(auth=BoxDeveloperTokenAuth(token=required("BOX_DEVELOPER_TOKEN")))
    state = load_state()
    ensure_metadata_template(client, state)
    template_id = os.getenv("BOX_FILE_REQUEST_TEMPLATE_ID", "").strip()
    webhook_address = os.getenv("BOX_WEBHOOK_ADDRESS", "").strip()
    if template_id and not state.get("file_request_id") and not template_id.isdecimal():
        raise RuntimeError(
            "BOX_FILE_REQUEST_TEMPLATE_ID must be the numeric ID from the Box File Request builder URL "
            "(`/filerequest/<id>`), not the public upload-link token (`/f/<token>`)."
        )
    if webhook_address:
        validate_webhook_address(webhook_address)

    root_id = ensure_folder(client, "0", "COI Decisions Demo")
    reference_id = ensure_folder(client, root_id, "Project Reference")
    intake_id = ensure_folder(client, root_id, "COI Intake")
    case_id = ensure_folder(client, root_id, "Case Files")
    requirements_id = ensure_upload(
        client,
        reference_id,
        SAMPLES_DIR / "project_insurance_requirements.pdf",
    )
    endorsement_id = ensure_upload(
        client,
        case_id,
        SAMPLES_DIR / "additional_insured_endorsement.pdf",
    )

    state.update(
        {
            "root_folder_id": root_id,
            "reference_folder_id": reference_id,
            "intake_folder_id": intake_id,
            "case_folder_id": case_id,
            "requirements_file_id": requirements_id,
            "sample_endorsement_file_id": endorsement_id,
        }
    )
    save_state(state)

    reviewer_id = os.getenv("BOX_REVIEWER_USER_ID", "").strip()
    if reviewer_id:
        add_reviewer(client, root_id, reviewer_id, state)

    file_request = None
    if state.get("file_request_id") or template_id:
        file_request = ensure_file_request(client, intake_id, template_id, state)

    webhook_id = None
    if webhook_address:
        webhook_id = ensure_upload_webhook(client, intake_id, webhook_address)
        state["webhook_id"] = webhook_id
        state["webhook_address"] = webhook_address
        save_state(state)

    print("Box demo folders and reference documents are ready.")
    print(f"Demo folder: {root_id}")
    print(f"COI Intake folder: {intake_id}")
    print(f"Open COI Intake in Box: https://app.box.com/folder/{intake_id}")
    print(f"Project requirements file: {requirements_id}")
    print(f"Metadata template: {METADATA_TEMPLATE_NAME} ({METADATA_TEMPLATE_KEY})")
    print(f"Case folder: {case_id}")
    if file_request:
        print(f"File Request link: {public_file_request_url(file_request.url)}")
    else:
        print("No File Request link exists yet. The Box API requires an existing request as a copy template.")
        print("Create a File Request on COI Intake in Box, set BOX_FILE_REQUEST_TEMPLATE_ID to its builder ID, and rerun seed.py.")
    if not webhook_id:
        print("Webhook not registered. Set BOX_WEBHOOK_ADDRESS to a public HTTPS callback and rerun seed.py.")
    if not reviewer_id:
        print("Optional: set BOX_REVIEWER_USER_ID and rerun seed.py to add the reviewer.")
    if file_request:
        print("Open the File Request link above to submit a sample COI.")
    print("Correction email delivery is a follow-on development step; this demo creates Box review tasks only.")


if __name__ == "__main__":
    main()
