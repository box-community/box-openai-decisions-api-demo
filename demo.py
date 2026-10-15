"""Receive Box upload webhooks and route COI uploads for human review."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time
from pathlib import Path
from threading import Lock

from box_sdk_gen import (
    AiItemBase,
    BoxClient,
    BoxDeveloperTokenAuth,
    CreateAiExtractStructuredFields,
    CreateCollaborationAccessibleBy,
    CreateCollaborationAccessibleByTypeField,
    CreateCollaborationItem,
    CreateCollaborationItemTypeField,
    CreateCollaborationRole,
    CreateTaskAction,
    CreateTaskAssignmentAssignTo,
    CreateTaskAssignmentTask,
    CreateTaskCompletionRule,
    CreateTaskItem,
    CreateTaskItemTypeField,
    CreateFileMetadataByIdScope,
    UpdateFileMetadataByIdRequestBody,
    UpdateFileMetadataByIdRequestBodyOpField,
    UpdateFileMetadataByIdScope,
    WebhooksManager,
)
from dotenv import load_dotenv
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / ".demo_state.json"
PENDING_EVENTS_PATH = BASE_DIR / ".pending_webhook_events.json"
STATE: dict | None = None
CLIENT: BoxClient | None = None
PENDING_LOCK = Lock()
WORKER = ThreadPoolExecutor(max_workers=1)

REQUIREMENT_FIELDS = [
    CreateAiExtractStructuredFields(
        key="project_completion_date",
        display_name="Project completion date",
        type="string",
        prompt="What is the required project completion date? Return the date as printed.",
    ),
    CreateAiExtractStructuredFields(
        key="general_liability_minimum",
        display_name="General liability minimum",
        type="string",
        prompt="What minimum general liability per-occurrence limit is required? Include currency and amount.",
    ),
    CreateAiExtractStructuredFields(
        key="auto_liability_minimum",
        display_name="Auto liability minimum",
        type="string",
        prompt="What minimum auto liability limit is required? Include currency and amount.",
    ),
    CreateAiExtractStructuredFields(
        key="workers_comp_required",
        display_name="Workers compensation requirement",
        type="string",
        prompt="What workers compensation coverage is required?",
    ),
    CreateAiExtractStructuredFields(
        key="additional_insured_requirement",
        display_name="Additional insured requirement",
        type="string",
        prompt="What additional insured evidence is required, and which parties must be named?",
    ),
]

COI_FIELDS = [
    CreateAiExtractStructuredFields(
        key="named_insured",
        display_name="Named insured",
        type="string",
        prompt="Who is the named insured on this certificate?",
    ),
    CreateAiExtractStructuredFields(
        key="policy_expiration_date",
        display_name="Policy expiration date",
        type="string",
        prompt="What is the policy expiration date? Return null or blank if absent or unreadable.",
    ),
    CreateAiExtractStructuredFields(
        key="general_liability_per_occurrence_limit",
        display_name="General liability per occurrence limit",
        type="string",
        prompt="What general liability per-occurrence limit is shown? Include currency and amount; blank if unclear.",
    ),
    CreateAiExtractStructuredFields(
        key="auto_liability_limit",
        display_name="Auto liability limit",
        type="string",
        prompt="What auto liability limit is shown? Include currency and amount; blank if unclear.",
    ),
    CreateAiExtractStructuredFields(
        key="workers_comp_evidence",
        display_name="Workers compensation evidence",
        type="string",
        prompt="What workers compensation evidence is shown? Quote a short phrase or say not found.",
    ),
    CreateAiExtractStructuredFields(
        key="additional_insured_statement",
        display_name="Additional insured statement on certificate",
        type="string",
        prompt="What does the certificate say about additional insured status? Quote a short phrase or say not found.",
    ),
    CreateAiExtractStructuredFields(
        key="endorsement_evidence",
        display_name="Separate endorsement evidence in this file",
        type="string",
        prompt="Is a separate additional-insured endorsement document actually included in this uploaded file? A certificate checkbox or words such as 'see endorsement' do not count. Answer yes, no, or unclear, and explain briefly.",
    ),
]

ROUTES = [
    {
        "value": "request_endorsement",
        "description": "The COI is readable and the needed coverage details are present, but the separate required additional-insured endorsement is missing. Create a review task so the project team can request the updated document.",
    },
    {
        "value": "specialist_review",
        "description": "A required date, limit, coverage detail, named party, or document is missing, unreadable, contradictory, or otherwise unclear. Assign a specialist to review.",
    },
    {
        "value": "ready_for_human_review",
        "description": "The file appears to contain the requested coverage details and separate endorsement. Create a review task.",
    },
]
DECISION_CONFIDENCE_THRESHOLD = 0.55


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} in .env before running the demo.")
    return value


def load_state() -> dict:
    if not STATE_PATH.exists():
        raise RuntimeError("Run `python3 seed.py` first to create the Box demo folders.")
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    temp_path = STATE_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(STATE_PATH)


def box_client() -> BoxClient:
    return BoxClient(auth=BoxDeveloperTokenAuth(token=required("BOX_DEVELOPER_TOKEN")))


def plain_dict(value: object) -> dict:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, dict):
        value = vars(value) if hasattr(value, "__dict__") else {}
    return {
        (key[2:] if isinstance(key, str) and key.startswith("d_") else key): item
        for key, item in value.items()
    }


def extract(client: BoxClient, file_id: str, fields: list) -> dict:
    response = client.ai.create_ai_extract_structured(
        items=[AiItemBase(id=file_id)],
        fields=fields,
    )
    return plain_dict(getattr(response, "answer", {}))


def box_metadata_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True, default=str)


def store_extracted_metadata(
    client: BoxClient,
    state: dict,
    file_id: str,
    requirements: dict,
    coi: dict,
    route: str,
    confidence: float,
    note: str,
) -> None:
    template_key = state.get("metadata_template_key")
    if not template_key:
        raise RuntimeError("Run `python3 seed.py` first to create the COI metadata template.")
    metadata = {
        **{f"requirements_{key}": box_metadata_text(value) for key, value in requirements.items()},
        **{f"coi_{key}": box_metadata_text(value) for key, value in coi.items()},
        "decision_route": route,
        "decision_confidence": confidence,
        "decision_note": note,
    }
    try:
        client.file_metadata.create_file_metadata_by_id(
            file_id,
            CreateFileMetadataByIdScope.ENTERPRISE,
            template_key,
            metadata,
        )
        action = "created"
    except Exception as error:
        error_message = str(error).lower()
        if "tuple_already_exists" not in error_message and "409" not in error_message:
            raise
        updates = [
            UpdateFileMetadataByIdRequestBody(
                op=UpdateFileMetadataByIdRequestBodyOpField.ADD,
                path=f"/{key}",
                value=value,
            )
            for key, value in metadata.items()
        ]
        client.file_metadata.update_file_metadata_by_id(
            file_id,
            UpdateFileMetadataByIdScope.ENTERPRISE,
            template_key,
            updates,
        )
        action = "updated"
    print(
        f"[workflow] Box metadata {action} using enterprise template {template_key} "
        f"for file {file_id} ({len(metadata)} fields)"
    )


def decide(requirements: dict, coi: dict, file_name: str) -> tuple[str, float, str]:
    prompt = """You route a subcontractor insurance document for an administrative completeness workflow.

Compare the extracted project requirements and uploaded file evidence. The file request currently contains this uploaded file only; do not assume a separate attachment exists. A COI checkbox or statement that an endorsement exists is not the endorsement itself. Use `request_endorsement` only when the other key evidence is readable and the endorsement is the main missing item. Use `specialist_review` for missing, unclear, or conflicting coverage details. Use `ready_for_human_review` only when all supplied evidence appears present. Never approve insurance or make a legal coverage determination.

Return the route that best fits the evidence and keep the rationale grounded in the supplied fields."""
    model = os.getenv("OPENAI_DECISIONS_MODEL", "gpt-6-luna")
    choices = ", ".join(item["value"] for item in ROUTES)
    print(
        f"[decisions] request file={file_name!r} model={model} "
        f"question=route choices=[{choices}]"
    )
    decision = OpenAI().decisions.create(
        model=model,
        input=(
            f"{prompt}\n\n"
            f"Uploaded filename: {file_name}\n\n"
            f"Project requirements extracted from the Box reference document:\n"
            f"{json.dumps(requirements, indent=2, default=str)}\n\n"
            f"COI evidence extracted from the Box upload:\n"
            f"{json.dumps(coi, indent=2, default=str)}"
        ),
        questions=[
            {
                "type": "choice",
                "name": "route",
                "instructions": "Choose one administrative workflow route based on the evidence and criteria.",
                "choices": ROUTES,
            }
        ],
    )
    if not decision.answers:
        note = "The Decisions API returned no answers; route to a human specialist."
        print(f"[decisions] fallback route=specialist_review reason={note}")
        return "specialist_review", 0.0, note

    answer = decision.answers[0]
    if answer.type != "choice":
        note = f"The Decisions API returned {answer.type!r}, not a route choice."
        print(f"[decisions] fallback route=specialist_review reason={note}")
        return "specialist_review", 0.0, note

    model_route = str(answer.choice)
    confidence = float(answer.confidence)
    probability_summary = []
    for item in getattr(answer, "probabilities", []) or []:
        if isinstance(item, dict):
            value = item.get("value")
            probability = item.get("probability")
        else:
            value = getattr(item, "value", None)
            probability = getattr(item, "probability", None)
        if value is not None and probability is not None:
            probability_summary.append(f"{value}={float(probability):.2f}")
    valid_routes = {item["value"] for item in ROUTES}
    fallback_reason = ""
    if model_route not in valid_routes:
        route = "specialist_review"
        fallback_reason = f"unsupported route {model_route!r}"
    elif confidence < DECISION_CONFIDENCE_THRESHOLD:
        route = "specialist_review"
        fallback_reason = "confidence below threshold"
    else:
        route = model_route

    note = (
        f"Decision fallback: {fallback_reason}."
        if fallback_reason
        else "OpenAI Decisions selected the route from the extracted evidence."
    )
    print(
        f"[decisions] result model_route={model_route} confidence={confidence:.2f} "
        f"threshold={DECISION_CONFIDENCE_THRESHOLD:.2f} final_route={route} "
        f"probabilities=[{', '.join(probability_summary) or 'unavailable'}]"
        + (f" fallback={fallback_reason}" if fallback_reason else "")
    )
    return route, confidence, note


def create_review_task(
    client: BoxClient,
    state: dict,
    file_id: str,
    file_name: str,
    route: str,
    confidence: float,
    context: str,
) -> str:
    message = (
        f"AI-assisted intake route: {route} ({confidence:.2f} confidence). "
        f"{context} Please inspect the source file and confirm the next step; do not treat the route as an insurance approval."
    )[:1000]
    task_ids = state.setdefault("review_task_ids", {})
    task_id = task_ids.get(file_id)
    if not task_id:
        task = client.tasks.create_task(
            CreateTaskItem(id=file_id, type=CreateTaskItemTypeField.FILE),
            action=CreateTaskAction.REVIEW,
            message=message,
            completion_rule=CreateTaskCompletionRule.ANY_ASSIGNEE,
        )
        if not task.id:
            raise RuntimeError("Box created a task without returning an ID.")
        task_id = task.id
        task_ids[file_id] = task_id
        save_state(state)
    reviewer_id = os.getenv("BOX_REVIEWER_USER_ID", "").strip()
    assigned_ids = state.setdefault("assigned_task_file_ids", [])
    if reviewer_id and file_id not in assigned_ids:
        client.task_assignments.create_task_assignment(
            CreateTaskAssignmentTask(id=task_id),
            CreateTaskAssignmentAssignTo(id=reviewer_id),
        )
        assigned_ids.append(file_id)
        save_state(state)
    print(f"Created review task {task_id} for {file_name}.")
    return task_id


def add_specialist(client: BoxClient, state: dict, file_id: str, user_id: str) -> None:
    collaboration_key = f"{file_id}:{user_id}"
    if collaboration_key in state.setdefault("specialist_file_collaborations", []):
        return
    try:
        client.user_collaborations.create_collaboration(
            CreateCollaborationItem(
                id=file_id,
                type=CreateCollaborationItemTypeField.FILE,
            ),
            CreateCollaborationAccessibleBy(
                id=user_id,
                type=CreateCollaborationAccessibleByTypeField.USER,
            ),
            CreateCollaborationRole.VIEWER,
            notify=False,
        )
    except Exception as error:
        if not any(token in str(error).lower() for token in ("409", "conflict", "already")):
            raise
        print(f"Insurance specialist {user_id} already has access to this file.")
    state["specialist_file_collaborations"].append(collaboration_key)
    save_state(state)
    print(f"Added insurance specialist {user_id} as a collaborator on file {file_id}.")


def process_file(client: BoxClient, state: dict, file_id: str, file_name: str) -> None:
    print(f"\n[workflow] processing Box file {file_name!r} (id={file_id})")
    print("[workflow] extracting project requirements with Box AI")
    requirements = extract(client, state["requirements_file_id"], REQUIREMENT_FIELDS)
    print("[workflow] extracting uploaded COI with Box AI")
    coi = extract(client, file_id, COI_FIELDS)
    route, confidence, note = decide(requirements, coi, file_name)
    reason = {
        "request_endorsement": "A required separate additional-insured endorsement appears to be missing.",
        "specialist_review": "Some required evidence is missing, unclear, or conflicting.",
        "ready_for_human_review": "The submitted evidence appears complete enough for a human review.",
    }[route]
    context = (
        f"{reason} Project requirements: completion by {requirements.get('project_completion_date')}; "
        f"general liability {requirements.get('general_liability_minimum')}; "
        f"auto liability {requirements.get('auto_liability_minimum')}; "
        f"workers compensation {requirements.get('workers_comp_required')}; "
        f"additional insured: {requirements.get('additional_insured_requirement')}. "
        f"Uploaded evidence: insured {coi.get('named_insured')}; policy expires {coi.get('policy_expiration_date')}; "
        f"general liability {coi.get('general_liability_per_occurrence_limit')}; "
        f"auto liability {coi.get('auto_liability_limit')}; workers compensation {coi.get('workers_comp_evidence')}; "
        f"additional insured statement {coi.get('additional_insured_statement')}; "
        f"separate endorsement {coi.get('endorsement_evidence')}."
    )
    print(f"\n{file_name}: {route} ({confidence:.2f} confidence)")
    print(f"  Box AI requirements: {json.dumps(requirements, default=str)}")
    print(f"  Box AI upload: {json.dumps(coi, default=str)}")
    print(f"  {note}")

    store_extracted_metadata(client, state, file_id, requirements, coi, route, confidence, note)

    specialist_id = os.getenv("BOX_SPECIALIST_USER_ID", "").strip()
    specialist_collaboration = route in {"request_endorsement", "specialist_review"} and bool(specialist_id)
    print(
        f"[workflow] actions review_task=create "
        f"specialist_collaboration={'create' if specialist_collaboration else 'skip'}"
    )
    if route in {"request_endorsement", "specialist_review"}:
        if specialist_id:
            add_specialist(client, state, file_id, specialist_id)
        else:
            print("Set BOX_SPECIALIST_USER_ID to add the insurance specialist to the uploaded file.")
    create_review_task(client, state, file_id, file_name, route, confidence, context)
    state.setdefault("processed_file_ids", []).append(file_id)
    save_state(state)


def pending_file_ids() -> list[str]:
    if not PENDING_EVENTS_PATH.exists():
        return []
    return json.loads(PENDING_EVENTS_PATH.read_text(encoding="utf-8"))


def save_pending_file_ids(file_ids: list[str]) -> None:
    temp_path = PENDING_EVENTS_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(file_ids, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(PENDING_EVENTS_PATH)


def finish_pending_file(file_id: str) -> None:
    with PENDING_LOCK:
        pending = pending_file_ids()
        if file_id in pending:
            pending.remove(file_id)
            save_pending_file_ids(pending)


def process_webhook_file(file_id: str) -> None:
    assert CLIENT is not None and STATE is not None
    success = False
    for attempt in range(3):
        try:
            if file_id in STATE.get("processed_file_ids", []):
                success = True
                break
            file = CLIENT.files.get_file_by_id(
                file_id,
                fields=["id", "name", "type", "parent"],
            )
            parent_id = str(getattr(getattr(file, "parent", None), "id", ""))
            if file.type != "file" or parent_id != str(STATE["intake_folder_id"]):
                print(f"Ignoring file {file_id}; it is not in the COI Intake folder.")
                success = True
                break
            process_file(CLIENT, STATE, file_id, file.name)
            success = True
            break
        except Exception as error:
            print(f"Could not process webhook file {file_id} (attempt {attempt + 1}/3): {error}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    if success:
        finish_pending_file(file_id)
    else:
        print(f"File {file_id} remains queued in {PENDING_EVENTS_PATH.name}; restart demo.py to retry it.")


def enqueue_webhook_file(file_id: str) -> bool:
    assert STATE is not None
    with PENDING_LOCK:
        pending = pending_file_ids()
        if file_id in STATE.get("processed_file_ids", []) or file_id in pending:
            return False
        pending.append(file_id)
        save_pending_file_ids(pending)
    WORKER.submit(process_webhook_file, file_id)
    return True


class BoxWebhookHandler(BaseHTTPRequestHandler):
    def respond(self, status: int, body: str = "") -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if encoded:
            self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.respond(200, "ok")
        else:
            self.respond(404, "not found")

    def do_POST(self) -> None:
        if self.path != "/box-webhook":
            self.respond(404, "not found")
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        headers = {key.lower(): value for key, value in self.headers.items()}
        try:
            valid = WebhooksManager.validate_message(
                body=body,
                headers=headers,
                primary_key=required("BOX_WEBHOOK_PRIMARY_KEY"),
                secondary_key=required("BOX_WEBHOOK_SECONDARY_KEY"),
            )
        except Exception as error:
            print(f"Could not validate Box webhook signature: {error}")
            valid = False
        if not valid:
            self.respond(401, "invalid Box webhook signature")
            return
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            self.respond(400, "invalid JSON")
            return
        source = event.get("source") or {}
        if event.get("trigger") == "FILE.UPLOADED" and source.get("type") == "file" and source.get("id"):
            file_id = str(source["id"])
            enqueue_webhook_file(file_id)
            print(f"Received Box FILE.UPLOADED webhook for file {file_id}.")
        self.respond(202, "accepted")

    def log_message(self, format: str, *args) -> None:
        print(f"Webhook receiver: {format % args}")


def main() -> None:
    global CLIENT, STATE
    load_dotenv(BASE_DIR / ".env")
    required("BOX_WEBHOOK_PRIMARY_KEY")
    required("BOX_WEBHOOK_SECONDARY_KEY")
    webhook_address = required("BOX_WEBHOOK_ADDRESS")
    STATE = load_state()
    webhook_id = STATE.get("webhook_id")
    if not webhook_id:
        raise RuntimeError("Run `python3 seed.py` with BOX_WEBHOOK_ADDRESS set to register the upload webhook.")
    if STATE.get("webhook_address") != webhook_address:
        raise RuntimeError("BOX_WEBHOOK_ADDRESS changed; rerun `python3 seed.py` to update the webhook.")
    CLIENT = box_client()
    for file_id in pending_file_ids():
        WORKER.submit(process_webhook_file, file_id)
    port = int(os.getenv("PORT", "8000"))
    print(f"Receiving Box FILE.UPLOADED events for COI Intake folder {STATE['intake_folder_id']}.")
    print(f"Webhook subscription configured by seed: {webhook_id} -> {webhook_address}")
    print(f"Listening on 0.0.0.0:{port}; public callback path is /box-webhook. Press Ctrl+C to stop.")
    ThreadingHTTPServer(("0.0.0.0", port), BoxWebhookHandler).serve_forever()


if __name__ == "__main__":
    main()
