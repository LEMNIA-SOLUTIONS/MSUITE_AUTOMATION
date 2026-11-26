"""Execution engine for the Send Notification automation step (push & in-app bell)."""

from __future__ import annotations

import json
import re

import frappe
from frappe.utils import cint

from msuite_automation.msuite_automation.constants import (
	NOTIFY_APP,
	NOTIFY_CHANNEL_KEY,
	NOTIFY_DEFAULT_CATEGORY,
	NOTIFY_DEFAULT_PRIORITY,
	NOTIFY_DEFAULT_ROUTE,
	NOTIFY_ERROR_EMPTY_TITLE,
	NOTIFY_ERROR_INVALID_TEMPLATE,
	NOTIFY_ERROR_NO_CHANNEL,
	NOTIFY_GUEST_USER,
	NOTIFY_MODE_ASSIGNEE,
	NOTIFY_MODE_FIELD,
	NOTIFY_MODE_RECORD_OWNER,
	NOTIFY_MODE_ROLE,
	NOTIFY_MODE_SPECIFIC_USERS,
	NOTIFY_PUSH_ERROR_LOG_TITLE,
	NOTIFY_TITLE_MAX,
	RESULT_SKIPPED_NO_RECIPIENT,
	SKIP_REASON_PUSH_UNAVAILABLE,
	StepOutcome,
	step_error,
	step_skipped,
)
from msuite_automation.msuite_automation.engine.filters import field_value
from msuite_automation.msuite_automation.engine.graph import g

_FIELD_SPLIT_RE = re.compile(r"[,;]")


def _doc_attr(doc, key: str, default=None):
	"""Read an attribute or dict key from a Document or dict safely.

	Args:
		doc: Frappe Document, dict, or object.
		key: Attribute or key name.
		default: Fallback when missing or None.

	Returns:
		Any: Resolved value or default.

	Raises:
		None.

	Side Effects:
		None.
	"""
	if doc is None:
		return default
	val = doc.get(key) if hasattr(doc, "get") else getattr(doc, key, None)
	return default if val is None else val


def _clean_dedupe(candidates: list) -> list[str]:
	"""Strip, drop blanks and Guest, and de-duplicate recipient IDs preserving order.

	Args:
		candidates: Raw recipient values (strings or None).

	Returns:
		list[str]: Ordered unique non-blank non-Guest identifiers.

	Raises:
		None.

	Side Effects:
		None.
	"""
	seen: set[str] = set()
	out: list[str] = []
	for item in candidates:
		if item is None:
			continue
		cleaned = str(item).strip()
		if not cleaned or cleaned == NOTIFY_GUEST_USER or cleaned in seen:
			continue
		seen.add(cleaned)
		out.append(cleaned)
	return out


def _enabled_users(candidates: list[str]) -> set[str]:
	"""Return the subset of candidate IDs that exist as enabled Users.

	Args:
		candidates: List of candidate user IDs.

	Returns:
		set[str]: User IDs present in User with enabled=1.

	Raises:
		None.

	Side Effects:
		Queries the User table once.
	"""
	if not candidates:
		return set()
	return set(
		frappe.get_all(
			"User",
			filters={"name": ["in", candidates], "enabled": 1},
			pluck="name",
		)
	)


def _parse_user_list(raw) -> list:
	"""Parse notify_users JSON string or list into candidate entries.

	Args:
		raw: JSON string or list of user IDs.

	Returns:
		list: Parsed list of raw entries, or empty list on invalid/empty input.

	Raises:
		None.

	Side Effects:
		None.
	"""
	if not raw:
		return []
	if isinstance(raw, list):
		return raw
	if isinstance(raw, str):
		try:
			parsed = json.loads(raw)
		except ValueError:
			return []
		return parsed if isinstance(parsed, list) else []
	return []


def resolve_notify_recipients(step, doc) -> list[str]:
	"""Resolve ordered, de-duplicated notification recipients for a Send Notification step.

	Supports Role, Specific Users, Record Owner, Assignee, and Field on Record modes.
	Drops blanks and Guest. All modes except Field on Record drop disabled or non-existent
	Users; Field on Record splits on comma/semicolon and preserves non-User emails for push.

	Args:
		step: Automation Step document or dict.
		doc: Trigger document or dict.

	Returns:
		list[str]: Ordered, de-duplicated recipient identifiers.

	Raises:
		None.

	Side Effects:
		Queries Has Role, ToDo, and User tables depending on the configured mode.
	"""
	mode = g(step, "notify_mode")
	candidates: list = []

	if mode == NOTIFY_MODE_ROLE:
		role = str(g(step, "notify_role") or "").strip()
		if role:
			candidates = frappe.get_all(
				"Has Role",
				filters={"role": role, "parenttype": "User"},
				pluck="parent",
				order_by="parent asc",
			)
	elif mode == NOTIFY_MODE_SPECIFIC_USERS:
		candidates = _parse_user_list(g(step, "notify_users"))
	elif mode == NOTIFY_MODE_RECORD_OWNER:
		owner = _doc_attr(doc, "owner")
		if owner:
			candidates = [owner]
	elif mode == NOTIFY_MODE_ASSIGNEE:
		doctype = _doc_attr(doc, "doctype")
		docname = _doc_attr(doc, "name")
		if doctype and docname:
			candidates = frappe.get_all(
				"ToDo",
				filters={"reference_type": doctype, "reference_name": docname, "status": "Open"},
				pluck="allocated_to",
				order_by="creation asc",
			)
	elif mode == NOTIFY_MODE_FIELD:
		field_name = str(g(step, "notify_field") or "").strip()
		raw = field_value(doc, field_name) if field_name else None
		if isinstance(raw, (list, tuple)):
			candidates = list(raw)
		elif raw is not None:
			candidates = _FIELD_SPLIT_RE.split(str(raw))

	cleaned = _clean_dedupe(candidates)
	if not cleaned or mode == NOTIFY_MODE_FIELD:
		return cleaned

	enabled = _enabled_users(cleaned)
	return [recipient for recipient in cleaned if recipient in enabled]


def _render_template_field(raw: str | None, context: dict, field_label: str) -> str:
	"""Render one Jinja template string with readable ValidationError on syntax/eval failure.

	Uses Frappe's sandboxed Jinja environment overlaid so missing/None fields render as
	blank strings rather than literal 'None' or DebugUndefined placeholders.

	Args:
		raw: Template source string (empty/None returns empty string).
		context: Jinja rendering context dictionary.
		field_label: Human-readable field name included in validation errors.

	Returns:
		str: Rendered string.

	Raises:
		frappe.ValidationError: If Jinja rendering fails or contains unsafe attribute access.

	Side Effects:
		None.
	"""
	if not raw:
		return ""
	text = str(raw)
	if ".__" in text:
		raise frappe.ValidationError(NOTIFY_ERROR_INVALID_TEMPLATE.format(field_label, "Illegal template"))
	try:
		from jinja2 import Undefined
		from frappe.utils.jinja import get_jenv, safe_render_flags

		jenv = get_jenv().overlay(undefined=Undefined, finalize=lambda v: "" if v is None else v)
		compiled = jenv.from_string(text)
		with safe_render_flags():
			return compiled.render(context)
	except Exception as err:
		raise frappe.ValidationError(NOTIFY_ERROR_INVALID_TEMPLATE.format(field_label, err)) from err


def render_notify(step, doc) -> dict:
	"""Render notification title, body, and route against ``{"doc": doc}``.

	Truncates the rendered title to NOTIFY_TITLE_MAX characters and falls back
	empty routes to NOTIFY_DEFAULT_ROUTE.

	Args:
		step: Automation Step document or dict.
		doc: Trigger document or dict.

	Returns:
		dict: ``{"title": str, "body": str, "route": str}``.

	Raises:
		frappe.ValidationError: If any notification template has invalid Jinja.

	Side Effects:
		None.
	"""
	context = {"doc": doc}
	title = _render_template_field(g(step, "notify_title"), context, "title")[:NOTIFY_TITLE_MAX]
	body = _render_template_field(g(step, "notify_body"), context, "body")
	route = _render_template_field(g(step, "notify_route"), context, "route").strip() or NOTIFY_DEFAULT_ROUTE
	return {"title": title, "body": body, "route": route}


def send_notification(ctx, step) -> StepOutcome:
	"""Execute a Send Notification step across push and/or in-app bell channels.

	Acquires the standard step deduplication lock via ``steps._send`` so duplicate
	runs and retries do not double-send. Missing ``msuite_notification`` app or
	disabled Push Notification Config does not fail the step: in-app still runs,
	and push-only steps skip with SKIP_REASON_PUSH_UNAVAILABLE. Per-recipient push
	enqueue exceptions are logged via ``frappe.log_error`` without failing the run
	or stopping delivery to remaining recipients.

	Args:
		ctx: Automation execution context containing automation, doc, value, discriminator, entry.
		step: Automation Step document or dict.

	Returns:
		StepOutcome: ``sent``, ``skipped: no recipient``, ``skipped: push unavailable``,
			``skipped: duplicate``, or ``error: ...``.

	Raises:
		channels.SendRetry: Propagated from ``steps._send`` when lock guard is unavailable.

	Side Effects:
		Acquires/releases deduplication cache locks, inserts Notification Log rows and publishes
		realtime events (in-app), and enqueues Push Notification Log jobs (push).
	"""
	from msuite_core.services.notifications import create_notification_log
	from msuite_automation.msuite_automation.engine.steps import _dedupe_key, _send

	use_push = bool(cint(g(step, "notify_push", 0)))
	use_in_app = bool(cint(g(step, "notify_in_app", 0)))
	if not use_push and not use_in_app:
		return step_error(NOTIFY_ERROR_NO_CHANNEL)

	doc = ctx.doc
	try:
		rendered = render_notify(step, doc)
	except frappe.ValidationError as err:
		return step_error(str(err))

	title = rendered["title"]
	if not title.strip():
		return step_error(NOTIFY_ERROR_EMPTY_TITLE)

	body = rendered["body"]
	route = rendered["route"]
	category = g(step, "notify_category") or NOTIFY_DEFAULT_CATEGORY
	priority = g(step, "notify_priority") or NOTIFY_DEFAULT_PRIORITY
	doctype = _doc_attr(doc, "doctype")
	docname = _doc_attr(doc, "name")
	dedupe_base = _dedupe_key(ctx, step, NOTIFY_CHANNEL_KEY)
	skip_reason: str | None = None

	def _deliver() -> bool:
		nonlocal skip_reason
		recipients = resolve_notify_recipients(step, doc)
		if not recipients:
			return False

		enabled_users = _enabled_users(recipients) if use_in_app else set()
		push_fn = None
		push_unavailable = False
		if use_push:
			if NOTIFY_APP in (frappe.get_installed_apps() or []):
				from msuite_notification.services.queue import enqueue_push

				push_fn = enqueue_push
			else:
				push_unavailable = True

		delivered = False
		push_queued = False
		# ponytail: one Log + one job per user; add a cap/batch only if a role grows past a few hundred
		for recipient in recipients:
			if use_in_app and recipient in enabled_users:
				create_notification_log(
					recipient=recipient,
					subject=title,
					doc_type=doctype,
					doc_name=docname,
					status=body,
					route=None,
					is_unread=True,
					msuite_route=route,
				)
				delivered = True

			if use_push and push_fn is not None:
				try:
					log_names = push_fn(
						recipient=recipient,
						title=title,
						body=body,
						data={"screen": route, "entity_type": doctype, "entity_name": docname},
						priority=priority,
						category=category,
						idempotency_key=f"{dedupe_base}:{recipient}",
						source_doctype=doctype,
						source_name=docname,
						enqueue_after_commit=True,
					)
				except Exception:
					frappe.log_error(title=NOTIFY_PUSH_ERROR_LOG_TITLE, message=frappe.get_traceback())
				else:
					if log_names:
						push_queued = True
						delivered = True

		if delivered:
			return True
		if not use_in_app and use_push and (push_unavailable or not push_queued):
			skip_reason = SKIP_REASON_PUSH_UNAVAILABLE
		return False

	outcome = _send(ctx, step, NOTIFY_CHANNEL_KEY, _deliver)
	if outcome == RESULT_SKIPPED_NO_RECIPIENT and skip_reason:
		return step_skipped(skip_reason)
	return outcome
