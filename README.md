# Box File Request + OpenAI Decisions: COI intake demo

A small US subcontractor onboarding demo. A subcontractor uploads a Certificate of Liability Insurance (COI) through a Box File Request. Box AI extracts evidence from the uploaded file and the project's insurance requirements. The OpenAI Decisions API routes the submission to a practical next step:

- flag a missing separate additional-insured endorsement for follow-up;
- assign an insurance specialist when a required value is missing or unclear; or
- create a review task when the evidence appears complete enough for a person to check.

Every route creates a Box review task with the extracted context. The uploaded file receives the extracted requirements and COI fields, route, confidence, and decision note in a named enterprise metadata template created by the seed. The specialist is added as a collaborator on the uploaded file for endorsement or evidence issues. The route is only an administrative signal; a person makes the coverage decision.

## The demo flow

```text
Box File Request upload
        ↓
Box FILE.UPLOADED webhook
        ↓
Box SDK + Box AI extract fields from the upload and requirements PDF
        ↓
OpenAI Decisions chooses a route
        ↓
Store extracted fields and decision on the uploaded file with the `COI Intake Review` metadata template
        ↓
Box review task + optional specialist collaboration
```

`seed.py` creates the Box demo content and registers a `FILE.UPLOADED` webhook for the intake folder when `BOX_WEBHOOK_ADDRESS` is set. Start `demo.py` to run the receiver. Uploading a file through the File Request triggers processing; there is no folder polling.

The workflow stores extracted requirement fields as `requirements_*`, certificate fields as `coi_*`, and the route, confidence, and decision note on the uploaded file using the enterprise template `COI Intake Review` (`coiIntakeReview`). `seed.py` creates the template in the authenticated user's Box enterprise on its first run and reuses it on later runs. It also adds any missing fields to that template. The metadata is visible on the uploaded file in Box.

## Sample files

All companies, people, project details, policy numbers, and limits in these artifacts are fictional. The COI summaries use an original coverage-grid layout based on familiar certificate fields, informed by the [Texas Department of Insurance sample](https://www.tdi.texas.gov/certificates/documentcr/s21357a.pdf). They are not official ACORD forms or proof of insurance.

| File | Use |
| --- | --- |
| `sample_documents/project_insurance_requirements.pdf` | Reference document already seeded into Box |
| `sample_documents/coi_missing_endorsement.pdf` | Upload to route a missing-endorsement submission to a Box review task |
| `sample_documents/additional_insured_endorsement.pdf` | Example of the separate evidence needed to complete the review |
| `sample_documents/coi_unclear_expiration.pdf` | Upload to route to specialist review because a required date is missing |
| `sample_documents/complete_submission_packet.pdf` | Two-page COI plus endorsement packet; route to human review, not approval |

## Prerequisites

- Python 3.10 or newer.
- A Box account and a Box Developer Token for the user who will own the demo folder. Developer Tokens expire and are intended for development.
- A Box user ID for the demo reviewer and, optionally, an insurance specialist. They should be existing users who can access the demo folder.
- An OpenAI API key with access to the Decisions API model configured below. The current example defaults to `gpt-6-luna`.

The Box user running the seed needs permission to create folders, upload files, write file metadata, create tasks, and add collaborations. Creating an enterprise metadata template also requires an admin or co-admin account with permission to create and edit metadata templates. The Box app must be authorized for the file and metadata operations used by the demo; if you change its scopes in Developer Console, reauthorize it before creating a new developer token. Box AI must be enabled for the account and the app must be allowed to use it.

## Setup

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
```

Edit `.env` and set:

```dotenv
BOX_DEVELOPER_TOKEN=...
OPENAI_API_KEY=...
BOX_REVIEWER_USER_ID=...
BOX_SPECIALIST_USER_ID=...
# Optional until you create a File Request in Box; use its builder ID, not its public upload token.
BOX_FILE_REQUEST_TEMPLATE_ID=...
BOX_WEBHOOK_ADDRESS=https://<your-public-host>/box-webhook
BOX_WEBHOOK_PRIMARY_KEY=...
BOX_WEBHOOK_SECONDARY_KEY=...
```

Use the webhook signing keys configured for your Box app. The webhook address must be publicly reachable over HTTPS on port `443`; do not append the local receiver port `:8000`.

`BOX_REVIEWER_USER_ID` and `BOX_SPECIALIST_USER_ID` are Box user IDs, not email addresses. Set `BOX_REVIEWER_USER_ID` to add the reviewer to the demo folder and assign the Box tasks; without it, tasks are created unassigned. The specialist is added to the uploaded file only when Decisions routes a submission for endorsement or specialist review. The fictional role names are listed in `sample_collaborators.json`; create or choose the corresponding users in your Box test environment.

For webhook delivery, the Box app must support Box V2 webhooks and have its primary and secondary webhook signature keys configured. The receiver listens locally on port `8000`, so use a public HTTPS tunnel such as ngrok while demonstrating locally. Start the tunnel before running the seed so Box can use its HTTPS address for the subscription.

## One-time Box seed

Start a public HTTPS tunnel in a separate terminal:

```bash
ngrok http 8000
```

Set `BOX_WEBHOOK_ADDRESS` in `.env` to the tunnel's HTTPS hostname plus `/box-webhook`, without `:8000`.

```bash
python3 seed.py
```

The script uses the Box Python SDK to create or reuse the enterprise `COI Intake Review` metadata template; create `COI Decisions Demo`, `Project Reference`, `COI Intake`, and `Case Files`; upload the requirements and sample endorsement PDFs; save their Box IDs and the metadata template key in `.demo_state.json`; add the reviewer if `BOX_REVIEWER_USER_ID` is set; and create or update the `FILE.UPLOADED` webhook for `COI Intake`. It is safe to rerun after an interrupted setup. The seed validates the callback address and saves the webhook ID and address in `.demo_state.json`. If `BOX_WEBHOOK_ADDRESS` is blank, the seed still creates the template, folders, and files but skips webhook registration; set it and rerun `python3 seed.py` to register it.

The template contains the requirement fields, certificate fields, and Decisions route, confidence, and note. Its field keys are prefixed with `requirements_` and `coi_` to keep the two document sources distinct. If Box returns a permission error while creating the template, use an admin or co-admin with the enterprise metadata-template permission and check that the app has been authorized for the required API operations.

The Box API does not create the first File Request from scratch. Create the initial request once in Box:

1. Open the seeded `COI Intake` folder.
2. Create a File Request for that folder.
3. Give it a clear title such as “Submit project insurance certificate.”
4. Open the request in its builder/editor and copy the ID from the URL, which looks like `/filerequest/1234567890`.
5. Set `BOX_FILE_REQUEST_TEMPLATE_ID=1234567890` in `.env` and rerun `python3 seed.py`. The seed uses the Box API to configure the request and save its public upload link. If you use a request from a different folder, the seed copies it to `COI Intake`.

Do not use the public upload URL's `/f/<token>` value as `BOX_FILE_REQUEST_TEMPLATE_ID`; it is a separate identifier. The File Request is used only to collect the upload in this demo; submitter email capture is not required.

## Run the demo

Run the receiver after the webhook has been registered by `seed.py`:

```bash
python3 demo.py
```

`demo.py` listens on port `8000` and validates incoming requests with the configured webhook signing keys. If you change `BOX_WEBHOOK_ADDRESS`, rerun `python3 seed.py` to update the Box subscription before restarting the receiver. Upload one sample PDF through the File Request link while the receiver is running:

- `coi_missing_endorsement.pdf` shows a contextual review task and a specialist file collaboration for a missing endorsement.
- `coi_unclear_expiration.pdf` shows specialist routing for missing evidence and a file collaboration.
- `complete_submission_packet.pdf` shows the ready-for-human-review route and a task.

The receiver validates Box webhook signatures using `BOX_WEBHOOK_PRIMARY_KEY` and `BOX_WEBHOOK_SECONDARY_KEY`. It stores processed Box file IDs and pending webhook events in local JSON state files. Do not put those files or `.env` under source control.

## Manual trigger

To run the processing flow for one uploaded file before the public webhook is ready, pass its Box file ID to the manual trigger script:

```bash
python3 manual_trigger.py <box-file-id>
```

The script queues the file through the same worker path used by webhook events. It still requires the seed state, Box access, Box AI, and an OpenAI API key. Processing is idempotent: a file already recorded as processed will not create another task.

## Follow-on development

Correction email delivery is a future project step. A later version could connect the `request_endorsement` route to an email service and send the submitter a contextual request for the missing endorsement. This demo has no email settings, preview, or sending logic; it stops at the Box review task and specialist collaboration.

## What to show

1. Open the project requirements PDF in Box and point out the limit and endorsement requirement.
2. Upload the COI using the File Request, without logging in as the subcontractor.
3. Watch Box AI extract a few values from both unstructured PDFs.
4. Show the Decisions route, the `COI Intake Review` fields in the uploaded file's Box metadata, the evidence in the task message, and the specialist's file collaboration.

The example uses a small local webhook receiver and JSON state files to keep the demo easy to inspect. It is a demo scaffold, not production workflow software; production needs durable idempotent state and a deployed, monitored webhook receiver. The first File Request is created once in Box because the Box API creates additional requests by copying an existing File Request.

## References

- [Box Python SDK](https://github.com/box/box-python-sdk)
- [Box File Requests](https://developer.box.com/guides/file-requests)
- [Box Copy File Request API](https://developer.box.com/reference/post-file-requests-id-copy/)
- [Box AI structured extraction](https://developer.box.com/reference/post-ai-extract-structured)
- [Applying metadata to a Box item](https://developer.box.com/guides/metadata/instances/create)
- [Creating a Box metadata template](https://developer.box.com/guides/metadata/templates/create)
- [Box security and API permissions](https://developer.box.com/guides/security)
- [Box metadata template admin permissions](https://support.box.com/hc/en-us/articles/1500005433721-Users-Groups-Settings)
- [Box Tasks API](https://developer.box.com/reference/post-tasks)
- [Box Collaborations API](https://developer.box.com/reference/post-collaborations)
- [OpenAI Decisions API](https://developers.openai.com/api/docs/guides/decisions)

## License

Licensed under the [Apache License 2.0](LICENSE).
