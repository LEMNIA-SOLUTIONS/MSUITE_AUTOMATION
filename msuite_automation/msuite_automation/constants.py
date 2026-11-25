from msuite_core.constants import (
	IG_INTERACTION_CAPTION_MENTION,
	IG_INTERACTION_COMMENT_MENTION,
)

DOCTYPE = "Messaging Automation"
CHILD_DOCTYPE = "Automation Variable Mapping"

DOCTYPE_EVENT = "DocType Event"
SCHEDULER_EVENT = "Scheduler Event"
LEAD_STATUS_EVENT = "Lead Status Event"
# Set on a record an automation step saves, so that save never starts other automations (no loops).
AUTOMATION_WRITE_FLAG = "from_messaging_automation_step"
# Doc flag: skip the unmapped-variable check on a draft save (pausing or duplicating must not be blocked by a template that changed in Meta).
SKIP_MAPPING_CHECK_FLAG = "skip_mapping_check"
# Scheduler/date-based runs execute as this user; Update Field saves then run as the automation's owner instead.
SYSTEM_USER = "Administrator"
OWNER_NO_WRITE_MESSAGE = "The automation owner {0} has no permission to update this record."
OWNER_UNUSABLE_MESSAGE = "Cannot run Update Field as the automation owner {0}: the user is missing or disabled."
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
OFFSET_DAYS_MIN = 1
OFFSET_DAYS_MAX = 10
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
# Frappe doc-event method names (keys of engine.triggers.TRIGGER_EVENT_MAP) that need a given docstatus / fire on delete.
SUBMIT_EVENT_METHODS = ("before_submit", "on_submit")
CANCEL_EVENT_METHODS = ("before_cancel", "on_cancel")
DELETE_EVENT_METHODS = ("on_trash", "after_delete")
SUBMITTED_SAVE_EVENT_METHODS = ("before_update_after_submit", "on_update_after_submit")  # only fire on a submitted doc
ANY_SAVE_EVENT_METHODS = ("before_insert", "after_insert", "before_validate", "validate", "on_update")
DOCSTATUS_SUBMITTED = 1
DOCSTATUS_CANCELLED = 2
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

# ── Structured step execution outcome (Task 4 / D7) ─────────────────────────
STEP_OUTCOME_KEY = "outcome_status"
STEP_STATUS_SENT = "sent"
STEP_STATUS_SKIPPED = "skipped"
STEP_STATUS_UPDATED = "updated"
STEP_STATUS_ADDED = "added"
STEP_STATUS_ERROR = "error"

SKIP_REASON_NO_RECIPIENT = "no recipient"
SKIP_REASON_DUPLICATE = "duplicate"
SKIP_REASON_ALREADY_SENT = "already sent"
SKIP_REASON_UNSUPPORTED_TYPE = "not a supported mention type"
SKIP_REASON_IG_LOGIN = "instagram login account"
SKIP_REASON_REPLY_REFUSED = "reply refused"
SKIP_REASON_MENTION_UNRESOLVED = "mention not resolved"
SKIP_REASON_NO_AUTHOR = "mention author unknown"
SKIP_REASON_OPTED_OUT = "opted out"
SKIP_REASON_NO_CONSENT = "no consent"
SKIP_REASON_NO_CONSENT_RECORD = "no consent record"
SKIP_REASON_TEMPLATES_DISABLED = "templates disabled"
SKIP_REASON_ALREADY_IN_LIST = "already in list"

RECIPIENT_SKIP_REASONS = (
	SKIP_REASON_NO_RECIPIENT,
	SKIP_REASON_OPTED_OUT,
	SKIP_REASON_NO_CONSENT,
	SKIP_REASON_NO_CONSENT_RECORD,
)


RESULT_SKIPPED_PREFIX = "skipped: "


class StepOutcome(str):
	"""Execution outcome of an automation step carrying a canonical outcome_status."""

	outcome_status: str

	def __new__(cls, display: str, outcome_status: str | None = None, **kwargs):
		obj = super().__new__(cls, display)
		obj.outcome_status = outcome_status or kwargs.get("status") or display
		return obj

	@property
	def reason(self) -> str | None:
		"""Extract reason from display string if applicable."""
		if self.outcome_status == STEP_STATUS_SKIPPED and self.startswith(RESULT_SKIPPED_PREFIX):
			return self[len(RESULT_SKIPPED_PREFIX):]
		if self.outcome_status == STEP_STATUS_ERROR and self.startswith("error: "):
			return self[len("error: "):]
		return None

	@classmethod
	def of(cls, status: str, reason: str | None = None) -> "StepOutcome":
		"""Construct a StepOutcome from canonical status and optional reason."""
		if status == STEP_STATUS_SKIPPED:
			display = f"{RESULT_SKIPPED_PREFIX}{reason}" if reason else "skipped"
		elif status == STEP_STATUS_ERROR:
			display = f"error: {reason}" if reason else "error"
		else:
			display = status
		return cls(display, outcome_status=status)


def step_sent() -> StepOutcome:
	"""Construct a successful 'sent' step outcome."""
	return StepOutcome.of(STEP_STATUS_SENT)


def step_skipped(reason: str) -> StepOutcome:
	"""Construct a 'skipped: <reason>' step outcome."""
	return StepOutcome.of(STEP_STATUS_SKIPPED, reason)


def step_updated() -> StepOutcome:
	"""Construct an 'updated' step outcome."""
	return StepOutcome.of(STEP_STATUS_UPDATED)


def step_added() -> StepOutcome:
	"""Construct an 'added' step outcome for recipient lists."""
	return StepOutcome.of(STEP_STATUS_ADDED)


def step_error(reason: str) -> StepOutcome:
	"""Construct an 'error: <reason>' step outcome."""
	return StepOutcome.of(STEP_STATUS_ERROR, reason)


RESULT_SENT = step_sent()
MARKETING_CATEGORY = "MARKETING"
WHATSAPP_SETTINGS_DOCTYPE = "WhatsApp Settings"
REQUIRE_OPT_IN_FIELD = "require_marketing_opt_in"  # WhatsApp Settings Check field

# Generate RESULT_SKIPPED_* from SKIP_REASON_* (one source)
for _name, _val in list(globals().items()):
	if _name.startswith("SKIP_REASON_"):
		globals()[f"RESULT_SKIPPED_{_name[len('SKIP_REASON_'):]}"] = step_skipped(_val)

# Skips that indicate non-delivery due to recipient reasons (opted out, no recipient, etc.).
# Only when all send steps encounter these reasons does the automation run become Skipped.
RESULT_SKIPPED_RECIPIENT_REASONS = tuple(step_skipped(r) for r in RECIPIENT_SKIP_REASONS)


def outcome_of(entry: dict | None) -> tuple[str | None, str | None]:
	"""Extract canonical (outcome_status, reason) from a run log entry.

	Supports both structured outcome_status/reason log entries and legacy result strings.
	"""
	if not isinstance(entry, dict):
		return None, None

	status = entry.get(STEP_OUTCOME_KEY)
	reason = entry.get("reason")

	if status is None:
		res = str(entry.get("result") or "")
		if res.startswith(RESULT_SKIPPED_PREFIX):
			status = STEP_STATUS_SKIPPED
			if reason is None:
				reason = res[len(RESULT_SKIPPED_PREFIX):].strip() or None
		elif res.startswith("error: "):
			status = STEP_STATUS_ERROR
			if reason is None:
				reason = res[len("error: "):].strip() or None
		elif res == "error":
			status = STEP_STATUS_ERROR
			if reason is None and entry.get("error"):
				reason = str(entry["error"])
		elif res in (STEP_STATUS_SENT, STEP_STATUS_UPDATED, STEP_STATUS_ADDED):
			status = res

	if status == STEP_STATUS_SKIPPED and reason is None:
		res = str(entry.get("result") or "")
		if res.startswith(RESULT_SKIPPED_PREFIX):
			reason = res[len(RESULT_SKIPPED_PREFIX):].strip() or None

	return status, reason
REASON_SEND_FAILED = "send failed"
RUN_STATUS_FAILED = "Failed"
RUN_STATUS_SKIPPED = "Skipped"
RUN_STATUS_COMPLETED = "Completed"
RUN_STATUS_WAITING = "Waiting"
DEAL_DOCTYPE = "CRM Deal"
DEAL_PRIMARY_CONTACT_MOBILE = "primary_contact_mobile"
DEAL_CONTACTS_CHILD_DOCTYPE = "CRM Contacts"
CONTACT_DOCTYPE = "Contact"
FIELD_MOBILE_NO = "mobile_no"
MESSAGE_STATUS_FAILED = "failed"  # Channel Message status, compared lower-case
DELIVERY_FAILED_DEFAULT_ERROR = "Message delivery failed"
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
