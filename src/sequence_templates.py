"""26 pre-built CRM sequence templates using the TCA (Trigger-Condition-Action) model.

Each template defines a DataSift automation sequence that can be created
via Playwright browser automation (datasift_uploader.py).

Organized into 5 folders:
  - Lead Management (6 sequences)
  - Acquisitions (6 sequences)
  - Transactions (6 sequences)
  - Deep Prospecting (4 sequences)
  - Default (4 sequences)

Usage:
  python src/main.py setup-sequences --folder all
  python src/main.py setup-sequences --folder lead-management --dry-run
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SequenceAction:
    """A single action in a sequence."""
    action_type: str = ""    # "change_status", "add_tag", "remove_tag", "create_task",
                              # "add_to_list", "remove_from_list", "assign_to",
                              # "clear_tasks", "clear_assignee", "send_sms",
                              # "move_card", "remove_card"
    value: str = ""           # Status name, tag name, task title, list name, etc.
    delay_days: int = 0       # Days to wait before executing (0 = immediate)


@dataclass
class SequenceTemplate:
    """Complete sequence definition."""
    name: str = ""
    folder: str = ""
    description: str = ""
    # Trigger
    trigger_type: str = ""    # "status_change", "tag_added", "tag_removed",
                              # "list_added", "list_removed", "task_completed",
                              # "task_created", "assignee_change",
                              # "card_created", "card_moved"
    trigger_value: str = ""   # Specific status/tag/list that triggers
    # Condition (optional filter)
    condition_type: str = ""  # "has_tag", "has_status", "in_list", "has_phone", etc.
    condition_value: str = ""
    condition_negate: bool = False  # If True, condition is "does NOT have"
    # Actions (executed in order)
    actions: list = field(default_factory=list)


# ── Lead Management Folder (6 sequences) ─────────────────────────────

LEAD_MANAGEMENT = [
    SequenceTemplate(
        name="01. New Lead Assignment",
        folder="Lead Management",
        description="Assigns new leads to team and creates intro task",
        trigger_type="status_change",
        trigger_value="New",
        actions=[
            SequenceAction("create_task", "Initial contact — call/text within 1 hour"),
            SequenceAction("add_tag", "new_lead"),
        ],
    ),
    SequenceTemplate(
        name="02. Hot Lead Alert",
        folder="Lead Management",
        description="Immediate action for hot-qualified leads",
        trigger_type="tag_added",
        trigger_value="hot",
        actions=[
            SequenceAction("change_status", "Hot Lead"),
            SequenceAction("create_task", "URGENT: Contact hot lead immediately"),
            SequenceAction("add_tag", "priority_contact"),
        ],
    ),
    SequenceTemplate(
        name="03. Follow-Up Reminder",
        folder="Lead Management",
        description="Creates follow-up task after initial contact",
        trigger_type="task_completed",
        trigger_value="Initial contact",
        actions=[
            SequenceAction("create_task", "Follow-up call/text", delay_days=3),
            SequenceAction("add_tag", "contacted"),
        ],
    ),
    SequenceTemplate(
        name="04. Stale Lead Recycler",
        folder="Lead Management",
        description="Moves inactive leads to nurture after 30 days",
        trigger_type="tag_added",
        trigger_value="stale_30d",
        actions=[
            SequenceAction("change_status", "Nurture"),
            SequenceAction("add_to_list", "Nurture"),
            SequenceAction("add_tag", "recycled"),
            SequenceAction("create_task", "Nurture check-in", delay_days=30),
        ],
    ),
    SequenceTemplate(
        name="05. Qualification Complete",
        folder="Lead Management",
        description="Routes qualified leads to closer",
        trigger_type="tag_added",
        trigger_value="qualified",
        actions=[
            SequenceAction("change_status", "Qualified"),
            SequenceAction("create_task", "Schedule appointment — qualified lead"),
            SequenceAction("remove_tag", "new_lead"),
        ],
    ),
    SequenceTemplate(
        name="06. DNC — Do Not Contact",
        folder="Lead Management",
        description="Removes DNC'd leads from all active workflows",
        trigger_type="tag_added",
        trigger_value="DNC",
        actions=[
            SequenceAction("change_status", "Do Not Contact"),
            SequenceAction("clear_tasks"),
            SequenceAction("clear_assignee"),
            SequenceAction("remove_from_list", "Foreclosure"),
            SequenceAction("remove_from_list", "Probate"),
            SequenceAction("remove_from_list", "Tax Sale"),
        ],
    ),
]

# ── Acquisitions Folder (6 sequences) ────────────────────────────────

ACQUISITIONS = [
    SequenceTemplate(
        name="07. Offer Sent",
        folder="Acquisitions",
        description="Tracks sent offers with follow-up",
        trigger_type="status_change",
        trigger_value="Offer Sent",
        actions=[
            SequenceAction("create_task", "Follow up on offer", delay_days=2),
            SequenceAction("add_tag", "offer_pending"),
        ],
    ),
    SequenceTemplate(
        name="08. Counter Received",
        folder="Acquisitions",
        description="Handles counter-offers",
        trigger_type="status_change",
        trigger_value="Counter",
        actions=[
            SequenceAction("create_task", "Analyze counter-offer — run updated comps"),
            SequenceAction("add_tag", "counter_received"),
        ],
    ),
    SequenceTemplate(
        name="09. Under Contract",
        folder="Acquisitions",
        description="Kicks off due diligence after contract acceptance",
        trigger_type="status_change",
        trigger_value="Under Contract",
        actions=[
            SequenceAction("create_task", "Order title search"),
            SequenceAction("create_task", "Schedule inspection", delay_days=1),
            SequenceAction("create_task", "Verify financing", delay_days=2),
            SequenceAction("add_tag", "under_contract"),
            SequenceAction("remove_tag", "offer_pending"),
        ],
    ),
    SequenceTemplate(
        name="10. Contract Fallen Through",
        folder="Acquisitions",
        description="Handles dead deals with nurture recycling",
        trigger_type="status_change",
        trigger_value="Dead Deal",
        actions=[
            SequenceAction("add_to_list", "Nurture"),
            SequenceAction("add_tag", "dead_deal"),
            SequenceAction("remove_tag", "under_contract"),
            SequenceAction("create_task", "90-day follow-up on dead deal", delay_days=90),
        ],
    ),
    SequenceTemplate(
        name="11. Closing Scheduled",
        folder="Acquisitions",
        description="Closing preparation checklist",
        trigger_type="status_change",
        trigger_value="Closing",
        actions=[
            SequenceAction("create_task", "Confirm closing date and location"),
            SequenceAction("create_task", "Verify clear title", delay_days=1),
            SequenceAction("create_task", "Wire transfer / cashier's check", delay_days=2),
            SequenceAction("add_tag", "closing_scheduled"),
        ],
    ),
    SequenceTemplate(
        name="12. Deal Closed",
        folder="Acquisitions",
        description="Post-closing automation",
        trigger_type="status_change",
        trigger_value="Closed",
        actions=[
            SequenceAction("add_tag", "closed_deal"),
            SequenceAction("remove_tag", "under_contract"),
            SequenceAction("remove_tag", "closing_scheduled"),
            SequenceAction("create_task", "Record deed / update records"),
        ],
    ),
]

# ── Transactions Folder (6 sequences) ────────────────────────────────

TRANSACTIONS = [
    SequenceTemplate(
        name="13. Sold Property Cleanup",
        folder="Transactions",
        description="Auto-cleanup when property tagged as Sold (ALREADY BUILT — build 1.0.23)",
        trigger_type="tag_added",
        trigger_value="Sold",
        actions=[
            SequenceAction("change_status", "Sold"),
            SequenceAction("remove_from_list", "Foreclosure"),
            SequenceAction("remove_from_list", "Probate"),
            SequenceAction("remove_from_list", "Tax Sale"),
            SequenceAction("clear_tasks"),
            SequenceAction("clear_assignee"),
        ],
    ),
    SequenceTemplate(
        name="14. Disposition Started",
        folder="Transactions",
        description="Kicks off marketing when property is ready to sell",
        trigger_type="status_change",
        trigger_value="For Sale",
        actions=[
            SequenceAction("create_task", "Create marketing package (photos, comps)"),
            SequenceAction("create_task", "List on MLS / send to buyer list", delay_days=1),
            SequenceAction("add_tag", "disposition_active"),
        ],
    ),
    SequenceTemplate(
        name="15. Buyer Assigned",
        folder="Transactions",
        description="Handles wholesale assignment to buyer",
        trigger_type="tag_added",
        trigger_value="buyer_matched",
        actions=[
            SequenceAction("create_task", "Draft assignment contract"),
            SequenceAction("create_task", "Coordinate buyer inspection", delay_days=1),
            SequenceAction("add_tag", "assignment_pending"),
        ],
    ),
    SequenceTemplate(
        name="16. Assignment Complete",
        folder="Transactions",
        description="Post-assignment cleanup",
        trigger_type="status_change",
        trigger_value="Assigned",
        actions=[
            SequenceAction("create_task", "Calculate and record profit"),
            SequenceAction("add_tag", "deal_done"),
            SequenceAction("remove_tag", "assignment_pending"),
        ],
    ),
    SequenceTemplate(
        name="17. Rehab Started",
        folder="Transactions",
        description="Tracks active rehab projects",
        trigger_type="status_change",
        trigger_value="In Rehab",
        actions=[
            SequenceAction("create_task", "Weekly rehab progress check"),
            SequenceAction("add_tag", "rehab_active"),
        ],
    ),
    SequenceTemplate(
        name="18. Rehab Complete",
        folder="Transactions",
        description="Transitions from rehab to sale",
        trigger_type="tag_added",
        trigger_value="rehab_complete",
        actions=[
            SequenceAction("change_status", "For Sale"),
            SequenceAction("create_task", "Schedule final walkthrough and photos"),
            SequenceAction("remove_tag", "rehab_active"),
        ],
    ),
]

# ── Deep Prospecting Folder (4 sequences) ────────────────────────────

DEEP_PROSPECTING = [
    SequenceTemplate(
        name="19. Needs Deep Prospecting",
        folder="Deep Prospecting",
        description="Routes records for research",
        trigger_type="tag_added",
        trigger_value="needs_dp",
        actions=[
            SequenceAction("create_task", "Deep prospecting — start Level 1 skip trace"),
            SequenceAction("add_tag", "dp_in_progress"),
        ],
    ),
    SequenceTemplate(
        name="20. DP Complete",
        folder="Deep Prospecting",
        description="Routes based on research findings",
        trigger_type="tag_added",
        trigger_value="dp_complete",
        actions=[
            SequenceAction("remove_tag", "dp_in_progress"),
            SequenceAction("create_task", "Review DP findings — route to marketing or archive"),
        ],
    ),
    SequenceTemplate(
        name="21. Heir Located",
        folder="Deep Prospecting",
        description="Begins marketing to identified heirs",
        trigger_type="tag_added",
        trigger_value="heir_found",
        actions=[
            SequenceAction("create_task", "Contact heir — begin niche sequential"),
            SequenceAction("add_tag", "heir_contact_pending"),
            SequenceAction("remove_tag", "needs_dp"),
        ],
    ),
    SequenceTemplate(
        name="22. Title Issue Flagged",
        folder="Deep Prospecting",
        description="Flags records needing attorney review",
        trigger_type="tag_added",
        trigger_value="title_issue",
        actions=[
            SequenceAction("create_task", "REFER TO TITLE ATTORNEY — curative work needed"),
            SequenceAction("change_status", "Title Review"),
            SequenceAction("add_tag", "attorney_needed"),
        ],
    ),
]

# ── Default Folder (4 sequences) ─────────────────────────────────────

DEFAULT = [
    SequenceTemplate(
        name="23. Welcome SMS",
        folder="Default",
        description="Sends intro SMS to new records with phone numbers",
        trigger_type="tag_added",
        trigger_value="new_record",
        condition_type="has_phone",
        actions=[
            SequenceAction("send_sms", "Hi, this is [Name] — I noticed your property and wanted to reach out..."),
            SequenceAction("add_tag", "sms_sent"),
        ],
    ),
    SequenceTemplate(
        name="24. Skip Trace Needed",
        folder="Default",
        description="Routes records without phone data to skip trace queue",
        trigger_type="tag_added",
        trigger_value="no_phone",
        actions=[
            SequenceAction("add_to_list", "Skip Trace Queue"),
            SequenceAction("create_task", "Run skip trace for contact info"),
        ],
    ),
    SequenceTemplate(
        name="25. Duplicate Detected",
        folder="Default",
        description="Handles duplicate records",
        trigger_type="tag_added",
        trigger_value="duplicate",
        actions=[
            SequenceAction("create_task", "Review and merge duplicate record"),
            SequenceAction("change_status", "Duplicate"),
        ],
    ),
    SequenceTemplate(
        name="26. Archive Inactive",
        folder="Default",
        description="Archives records with no activity in 180 days",
        trigger_type="tag_added",
        trigger_value="inactive_180d",
        actions=[
            SequenceAction("change_status", "Archived"),
            SequenceAction("remove_from_list", "Foreclosure"),
            SequenceAction("remove_from_list", "Probate"),
            SequenceAction("remove_from_list", "Tax Sale"),
            SequenceAction("clear_tasks"),
        ],
    ),
]

# ── All templates ─────────────────────────────────────────────────────

ALL_TEMPLATES = LEAD_MANAGEMENT + ACQUISITIONS + TRANSACTIONS + DEEP_PROSPECTING + DEFAULT

FOLDER_MAP = {
    "lead-management": LEAD_MANAGEMENT,
    "acquisitions": ACQUISITIONS,
    "transactions": TRANSACTIONS,
    "deep-prospecting": DEEP_PROSPECTING,
    "default": DEFAULT,
    "all": ALL_TEMPLATES,
}


def get_templates(folder: str = "all") -> list[SequenceTemplate]:
    """Get sequence templates by folder name."""
    return FOLDER_MAP.get(folder.lower(), ALL_TEMPLATES)


def list_templates() -> str:
    """Return a formatted string listing all templates."""
    lines = []
    current_folder = ""
    for t in ALL_TEMPLATES:
        if t.folder != current_folder:
            lines.append(f"\n{t.folder}:")
            current_folder = t.folder
        actions_str = ", ".join(a.action_type for a in t.actions)
        lines.append(f"  {t.name}")
        lines.append(f"    Trigger: {t.trigger_type} = {t.trigger_value}")
        lines.append(f"    Actions: {actions_str}")
    return "\n".join(lines)


def preview_sequence(template: SequenceTemplate) -> dict:
    """Return a dict preview of what would be created in DataSift."""
    return {
        "name": template.name,
        "folder": template.folder,
        "description": template.description,
        "trigger": f"{template.trigger_type}: {template.trigger_value}",
        "condition": f"{template.condition_type}: {template.condition_value}" if template.condition_type else "None",
        "actions": [
            {"type": a.action_type, "value": a.value, "delay_days": a.delay_days}
            for a in template.actions
        ],
    }
