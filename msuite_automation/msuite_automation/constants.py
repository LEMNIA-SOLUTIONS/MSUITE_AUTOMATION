from msuite_core.constants import (
	IG_INTERACTION_CAPTION_MENTION,
	IG_INTERACTION_COMMENT_MENTION,
)

DOCTYPE = "Messaging Automation"
CHILD_DOCTYPE = "Automation Variable Mapping"

DOCTYPE_EVENT = "DocType Event"
SCHEDULER_EVENT = "Scheduler Event"
LEAD_STATUS_EVENT = "Lead Status Event"
ORDER_WORKFLOW_EVENT = "Order Workflow Event"
NOTIFICATION_TYPES = (DOCTYPE_EVENT, SCHEDULER_EVENT)
ALL_TYPES = (DOCTYPE_EVENT, SCHEDULER_EVENT, LEAD_STATUS_EVENT, ORDER_WORKFLOW_EVENT)

# Doctypes that have an on_update hook into lead_status_automation (hooks.py).
LEAD_STATUS_DOCTYPES = ("CRM Lead", "Opportunity", "Customer", "Quotation", "Issue")
ORDER_DOCTYPES = ("Sales Order", "Delivery Note")

FEATURE_BY_TYPE = {
	DOCTYPE_EVENT: "notifications",
	SCHEDULER_EVENT: "notifications",
	LEAD_STATUS_EVENT: "lead_automation",
	ORDER_WORKFLOW_EVENT: "order_automation",
}


# ── Unified model (2026-09 automation builder) ──────────────────────────
RUN_DOCTYPE = "Messaging Automation Run"
OUTBOX_DOCTYPE = "Automation Event Outbox"

# ── Engine limits (one home; behavior-neutral names for values that used to be inline) ──
QUEUE_SHORT = "short"  # RQ queue for every automation job
RUN_CLAIM_TTL_SECONDS = 15 * 60  # per-document run claim; covers the gap between the duplicate check and the run row's commit
STUCK_SWEEP_BATCH = 200  # Running runs failed per sweeper pass
WAIT_KEY_MAX_LENGTH = 140  # must equal the `wait_key` field length in Automation Run Path (pinned by a test)
EVENT_CANDIDATE_LIMIT = 100  # records of the other doctype read per Wait-for-Event check
WALK_STEP_LIMIT = 500  # steps one run may take before it is stopped as a loop
DEDUPE_LOCK_TTL_SECONDS = 86400  # default life of the duplicate-send lock
RETRY_NOTE_MAX = 200  # characters of the error kept in a "retrying" log line
RUN_ERROR_MAX = 500  # characters of an error kept in a run log entry
PREVIEW_ERROR_MAX = 300  # characters of an error shown in the dry-run preview
RUN_LIST_DEFAULT_LIMIT = 10  # runs returned by get_runs when no valid limit is given
RUN_LIST_MAX_LIMIT = 50  # most runs get_runs returns in one call

DOCUMENT_EVENT = "Document Event"
FIELD_CHANGE = "Field Change"
DATE_BASED = "Date Based"
SCHEDULE = "Schedule"
TRIGGER_TYPES = (DOCUMENT_EVENT, FIELD_CHANGE, DATE_BASED, SCHEDULE)

SEND_WHATSAPP = "Send WhatsApp"
SEND_EMAIL = "Send Email"
SEND_SMS = "Send SMS"
WAIT = "Wait"
WAIT_EVENT = "Wait for Event"
IF_ELSE = "If/Else"
UPDATE_FIELD = "Update Field"
ADD_TO_LIST = "Add to Recipient List"
SWITCH = "Switch"
SEND_IG_PUBLIC_REPLY = "Send IG Public Reply"
SEND_IG_CONSENT_TEMPLATE = "Send IG Consent Template"
STEP_TYPES = (SEND_WHATSAPP, SEND_EMAIL, SEND_SMS, WAIT, WAIT_EVENT, IF_ELSE, SWITCH, UPDATE_FIELD, ADD_TO_LIST, SEND_IG_PUBLIC_REPLY, SEND_IG_CONSENT_TEMPLATE)
SEND_STEPS = (SEND_WHATSAPP, SEND_EMAIL, SEND_SMS)
ACTION_TYPES = (SEND_WHATSAPP, SEND_EMAIL, SEND_SMS, UPDATE_FIELD, ADD_TO_LIST, SEND_IG_PUBLIC_REPLY, SEND_IG_CONSENT_TEMPLATE)

RESULT_SENT = "sent"
RESULT_SKIPPED_PREFIX = "skipped: "
RESULT_SKIPPED_DUPLICATE = "skipped: duplicate"
RESULT_SKIPPED_UNSUPPORTED_TYPE = "skipped: not a supported mention type"
RESULT_SKIPPED_IG_LOGIN = "skipped: instagram login account"
RESULT_SKIPPED_REPLY_REFUSED = "skipped: reply refused"
RESULT_SKIPPED_MENTION_UNRESOLVED = "skipped: mention not resolved"
RESULT_SKIPPED_NO_AUTHOR = "skipped: mention author unknown"
RESULT_SKIPPED_NO_CONSENT_RECORD = "skipped: no consent record"
REASON_SEND_FAILED = "send failed"
RETRY_MENTION_PENDING = "mention resolution pending"
CONTEXT_MENTION_REPLY = "mention reply"

# Canvas graph: path state inside one Messaging Automation Run.
PATH_RUNNING = "Running"
PATH_WAITING = "Waiting"
PATH_AT_JOIN = "At join"
PATH_ENDED = "Ended"
WAIT_SIGNAL = "__wait__"

EVERY_TIME = "Every time"
ONCE_PER_DAY = "Once per day"
ONCE_PER_DOCUMENT = "Once per document"


# Field Change on these doctypes is sold as "lead_automation" / "order_automation";
# everything else is "notifications". All three fall back to the WA product (plan_enforcer).
def feature_for(trigger_type: str | None, reference_doctype: str | None) -> str:
	if trigger_type == FIELD_CHANGE and reference_doctype in LEAD_STATUS_DOCTYPES:
		return "lead_automation"
	if trigger_type == FIELD_CHANGE and reference_doctype in ORDER_DOCTYPES:
		return "order_automation"
	return "notifications"


def plan_features_for(doc) -> list[str]:
	"""The plan features a Messaging Automation needs: one per kind of trigger.

	This app's plan gate in plan_gates.py uses it.
	"""
	return sorted({
		feature_for(getattr(t, "trigger_type", None) or t.get("trigger_type"), getattr(t, "reference_doctype", None) or t.get("reference_doctype"))
		for t in (getattr(doc, "triggers", None) or [])
	}) or [FEATURE_BY_TYPE.get(getattr(doc, "notification_type", None), "notifications")]


# ── Instagram Mention Consent Flow ──────────────────────────────────────────

IG_CONSENT_DEFAULT_EXPIRY_DAYS = 7
IG_CONSENT_MIN_EXPIRY_DAYS = 1  # smallest valid Send IG Public Reply expiry
IG_REPLY_SUPPORTED_TYPES = (IG_INTERACTION_COMMENT_MENTION, IG_INTERACTION_CAPTION_MENTION)

IG_CONSENT_NOTE_IG_LOGIN = "Instagram Login account: public mention replies are not available"
