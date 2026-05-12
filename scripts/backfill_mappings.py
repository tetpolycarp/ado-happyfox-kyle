"""
Backfill Mappings — seed the mapping table from existing tickets.

One-off script to create mapping records for ADO work items and HappyFox tickets
that were created before the integration was deployed.

Usage:
    python scripts/backfill_mappings.py
"""

from __future__ import annotations

# TODO: Implement when needed
# - Query all Client Story work items from ADO
# - For each, check if Support Ticket Number is populated
# - If yes, create a mapping record linking ado_child_id ↔ hf_ticket_id

if __name__ == "__main__":
    print("Backfill mappings script — not yet implemented")
