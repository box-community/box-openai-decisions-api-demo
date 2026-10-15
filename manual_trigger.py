"""Manually queue one Box file for processing without a webhook event."""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

import demo


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the COI review workflow for one Box file ID."
    )
    parser.add_argument("file_id", help="Box file ID in the COI Intake folder")
    args = parser.parse_args()
    if not args.file_id.isdecimal():
        parser.error("file_id must be the Box file's numeric ID, not a File Request URL token")

    load_dotenv(demo.BASE_DIR / ".env")
    demo.STATE = demo.load_state()
    demo.CLIENT = demo.box_client()
    file_id = args.file_id
    if file_id in demo.STATE.get("processed_file_ids", []):
        task_id = demo.STATE.get("review_task_ids", {}).get(file_id)
        print(f"File {file_id} was already processed. Existing review task: {task_id or 'none'}.")
        return 0

    queued = demo.enqueue_webhook_file(file_id)
    if not queued:
        if file_id in demo.pending_file_ids():
            print(f"File {file_id} is already queued; retrying it now.")
            demo.WORKER.submit(demo.process_webhook_file, file_id)
        else:
            print(f"File {file_id} is already queued or processed.")

    demo.WORKER.shutdown(wait=True)
    task_id = demo.STATE.get("review_task_ids", {}).get(file_id)
    if file_id in demo.STATE.get("processed_file_ids", []):
        print(f"Processed file {file_id}; review task: {task_id or 'none'}.")
        return 0

    if file_id in demo.pending_file_ids():
        print(f"Processing failed. File {file_id} remains queued for retry.")
    else:
        print(f"File {file_id} was not processed. Confirm it is in the seeded COI Intake folder.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
