"""
Replay Dead Letter Queue messages.

Reads messages from a Service Bus DLQ, resubmits them to the main queue, and
removes the DLQ copy after a successful re-enqueue. Designed for bulk recovery
after an outage (e.g. HappyFox API downtime) where many messages landed in the
DLQ and need to be reprocessed in one pass.

Usage
-----
  # Preview what would be replayed — nothing is modified
  python scripts/replay_dlq.py --queue ado-child-events --dry-run

  # Replay up to 500 messages from the child queue's DLQ
  python scripts/replay_dlq.py --queue ado-child-events --max 500

  # Replay both integration queues back-to-back
  python scripts/replay_dlq.py --all --max 1000

Exit code is non-zero if any messages failed to replay.

Notes
-----
* Re-enqueue happens BEFORE DLQ completion, so messages are not lost if the
  script is interrupted. Downstream processors are idempotent so accidental
  duplicates are safe.
* Requires AZURE_SERVICEBUS_CONNECTION_STRING (and the rest of the app
  settings needed by src.integration.config) in the shell environment.
"""

from __future__ import annotations

import argparse
import sys

from src.integration.config import settings
from src.integration.services.servicebus_client import replay_dead_letter_messages
from src.integration.utils.logging_config import configure_logging


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay Service Bus DLQ messages.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--queue",
        help="Specific queue whose DLQ to drain (e.g. ado-parent-events).",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Replay DLQs for both integration queues (parent + child).",
    )
    p.add_argument(
        "--max",
        type=int,
        default=500,
        help="Maximum messages to replay total (default 500).",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Messages per receive batch (default 10).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Peek-only — log what would be replayed without modifying queues.",
    )
    return p.parse_args()


def _queues_to_process(args: argparse.Namespace) -> list[str]:
    if args.all:
        return [
            settings.ado_parent_events_queue,
            settings.ado_child_events_queue,
        ]
    return [args.queue]


def main() -> int:
    args = _parse_args()
    configure_logging(settings.log_level)

    print(f"Mode: {'DRY RUN' if args.dry_run else 'REPLAY'}")
    print(f"Max messages: {args.max}")
    print()

    total_replayed = 0
    total_failed = 0

    for queue in _queues_to_process(args):
        print(f"--- {queue} ---")
        result = replay_dead_letter_messages(
            queue_name=queue,
            max_messages=args.max,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
        print(
            f"  seen={result['remaining_seen']}  "
            f"replayed={result['replayed']}  failed={result['failed']}"
        )
        print()
        total_replayed += result["replayed"]
        total_failed += result["failed"]

    print(f"TOTAL: replayed={total_replayed} failed={total_failed}")

    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
