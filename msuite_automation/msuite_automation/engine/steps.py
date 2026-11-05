"""Per-step execution handlers and branch resolution."""

from __future__ import annotations

import functools
import json

import frappe
import requests

from msuite_core.constants import (
	IG_CONSENT_DM_RECEIVED,
	IG_CONSENT_DOCTYPE,
	IG_CONSENT_REPLY_FAILED,
	IG_CONSENT_REPLY_SENT,
	IG_CONSENT_REPLY_SKIPPED,
	IG_INTERACTION_COMMENT_MENTION,
	IG_MENTION_NO_AUTHOR,
	IG_MENTION_PENDING,
	IG_MENTION_UNRESOLVABLE,
	SOCIAL_MENTION_DOCTYPE,
)
from msuite_automation.msuite_automation.constants import (
	IG_CONSENT_DEFAULT_EXPIRY_DAYS,
	IG_CONSENT_NOTE_IG_LOGIN,
	IG_REPLY_SUPPORTED_TYPES,
)
from msuite_inbox.inbox.services import ig_consent
from msuite_inbox.inbox.services.channels import messenger_templates
from msuite_inbox.inbox.services.mentions import graph
from msuite_automation.msuite_automation.constants import (
	ADD_TO_LIST,
	CONTEXT_MENTION_REPLY,
	ONCE_PER_DAY,
	ONCE_PER_DOCUMENT,
	REASON_SEND_FAILED,
	RESULT_SENT,
	RESULT_SKIPPED_DUPLICATE,
	RESULT_SKIPPED_IG_LOGIN,
	RESULT_SKIPPED_MENTION_UNRESOLVED,
	RESULT_SKIPPED_NO_AUTHOR,
	RESULT_SKIPPED_NO_CONSENT_RECORD,
	RESULT_SKIPPED_PREFIX,
	RESULT_SKIPPED_REPLY_REFUSED,
	RESULT_SKIPPED_TEMPLATES_DISABLED,
	RESULT_SKIPPED_UNSUPPORTED_TYPE,
	RETRY_MENTION_PENDING,
	SEND_EMAIL,
	SEND_IG_CONSENT_TEMPLATE,
	SEND_IG_PUBLIC_REPLY,
	SEND_SMS,
	SEND_WHATSAPP,
	UPDATE_FIELD,
)
from msuite_automation.msuite_automation.engine import channels
from msuite_automation.msuite_automation.engine.filters import rules_pass
from msuite_automation.msuite_automation.engine.graph import g
from msuite_core.integrations.meta import graph as messenger
from msuite_core.services.connections.account_health import flag_if_auth_failure
from msuite_core.services.connections import tokens
from msuite_core.services.validation import phone as phone_validator


def branch_of(ctx, step) -> str:
	rules = json.loads(step.condition_rules or "[]")
	return "Yes" if rules_pass(rules, step.condition_logic or "All", ctx.doc) else "No"


def _dedupe_key(ctx, step, channel):
	a = ctx.automation
	return f"order_wf_dedupe:{a.name}:{step.step_id}:{ctx.doc.doctype}:{ctx.doc.name}:{ctx.value}:{ctx.discriminator}:{channel}"


def _send(ctx, step, channel, fn):
	a = ctx.automation
	lock = _dedupe_key(ctx, step, channel) if a.send_frequency not in (ONCE_PER_DAY, ONCE_PER_DOCUMENT) and ctx.value else None
	if lock:
		try:
			if not channels.acquire_lock(lock):
				return "skipped: duplicate"
		except channels.LockUnavailable as e:
			raise channels.SendRetry("transient", f"duplicate-send guard unavailable: {e}") from e
	try:
		ok = fn()
	except channels.RecipientOptedOut:
		if lock:
			channels.release_lock(lock)
		return "skipped: opted out"
	except Exception:
		if lock:
			channels.release_lock(lock)
		raise
	if not ok and lock:
		channels.release_lock(lock)
	return "sent" if ok else "skipped: no recipient"


def update_value(doc, step):
	doctype = getattr(doc, "doctype", None) or (doc.get("doctype") if hasattr(doc, "get") else None)
	meta = frappe.get_meta(doctype) if doctype else None
	df = meta.get_field(step.update_field) if meta else None
	value = step.update_value
	if df and df.fieldtype in ("Int", "Long Int", "Check"):
		return frappe.utils.cint(value)
	if df and df.fieldtype in ("Float", "Currency", "Percent"):
		return frappe.utils.flt(value)
	return value


def set_in_memory(doc, field, value):
	if callable(getattr(doc, "set", None)):
		doc.set(field, value)
	else:
		doc[field] = value


def _is_marketing_opted_out(ctx, step) -> bool:
	tpl_name = getattr(step, "template", None) or (step.get("template") if isinstance(step, dict) else None)
	if not tpl_name:
		return False
	category = frappe.db.get_value("WhatsApp Templates", tpl_name, "category")
	if not category and hasattr(step, "category"):
		category = step.category
	elif not category and isinstance(step, dict):
		category = step.get("category")
	if (category or "").strip().upper() != "MARKETING":
		return False

	a, doc = ctx.automation, ctx.doc
	phone = (ctx.get("phone") if hasattr(ctx, "get") else None) or channels.resolve_phone(a, doc)
	if not phone:
		return False

	parsed = phone_validator.normalize(phone)
	phone_e164 = parsed.get("e164") or phone
	# STOP from a number outside every Audience only flags the Contact, so check both stores.
	return channels.whatsapp_opted_out(phone_e164) or any(
		frappe.db.exists("Contact", {col: val, "unsubscribed_whatsapp": 1})
		for col, val in (("phone_e164", phone_e164), ("mobile_no", phone))
	)


def _require_mention(doc, step_type: str) -> None:
	"""Ensure a step runs against a Social Mention document.

	Args:
		doc: The automation's trigger document.
		step_type: Step type label used in the error message.

	Raises:
		ValueError: If doc is not a Social Mention.
	"""
	if getattr(doc, "doctype", None) != SOCIAL_MENTION_DOCTYPE:
		raise ValueError(f"{step_type} requires a {SOCIAL_MENTION_DOCTYPE} document, got {getattr(doc, 'doctype', None)}")


def send_ig_public_reply(ctx, step) -> str:
	"""Post a public comment replying to an Instagram mention and create pending consent.

	Args:
		ctx: Automation execution context containing doc, automation, run, etc.
		step: Automation Step document/dict containing step configuration.

	Returns:
		str: Execution result ("sent", "skipped: ...").

	Raises:
		ValueError: If doc is not a Social Mention, template is invalid, or token is missing.
		channels.SendRetry: If a transient Meta error occurs.
		requests.HTTPError: If Meta returns an authentication error.

	Side Effects:
		Acquires and may release deduplication lock in cache. Calls Meta Graph API
		to post a comment reply. Inserts an Instagram Consent Response document.
	"""
	doc = ctx.doc
	_require_mention(doc, SEND_IG_PUBLIC_REPLY)

	interaction_type = getattr(doc, "interaction_type", None)
	if interaction_type not in IG_REPLY_SUPPORTED_TYPES:
		return RESULT_SKIPPED_UNSUPPORTED_TYPE

	account = frappe.get_doc("Social Account", getattr(doc, "social_account", None))

	if not messenger.uses_instagram_login(account):
		resolution = ig_consent.ensure_mention_resolved(doc)
		if resolution == IG_MENTION_NO_AUTHOR:
			return RESULT_SKIPPED_NO_AUTHOR
		if resolution == IG_MENTION_UNRESOLVABLE:
			return RESULT_SKIPPED_MENTION_UNRESOLVED
		if resolution == IG_MENTION_PENDING:
			raise channels.SendRetry("transient", f"{RETRY_MENTION_PENDING}: {doc.name}")

	lock_key = f"ig_mention_reply:{doc.name}"
	try:
		if not channels.acquire_lock(lock_key):
			return RESULT_SKIPPED_DUPLICATE
	except channels.LockUnavailable as e:
		raise channels.SendRetry("transient", f"duplicate-send guard unavailable: {e}") from e

	if frappe.db.exists(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}):
		return RESULT_SKIPPED_DUPLICATE

	step_expiry = g(step, "ig_expiry_days")
	create_pending = functools.partial(
		ig_consent.create_pending,
		doc.name,
		automation=ctx.automation.name,
		run=ctx.run,
		expiry_days=frappe.utils.cint(step_expiry) if step_expiry else IG_CONSENT_DEFAULT_EXPIRY_DAYS,
	)

	posted = False
	try:
		if messenger.uses_instagram_login(account):
			create_pending(reply_status=IG_CONSENT_REPLY_SKIPPED, reply_note=IG_CONSENT_NOTE_IG_LOGIN)
			return RESULT_SKIPPED_IG_LOGIN

		try:
			rendered = frappe.render_template(
				g(step, "ig_reply_text", "") or "",
				{"username": getattr(doc, "username", None) or "", "doc": doc.as_dict()},
			)
		except Exception as err:
			raise ValueError(f"Invalid template in ig_reply_text: {err}") from err

		token = tokens.get_valid_token(account)
		if not token or not str(token).strip():
			raise ValueError(f"No valid access token found for Social Account {account.name}")

		try:
			reply_id = graph.post_mention_reply(
				account.account_id,
				token,
				message=rendered,
				media_id=getattr(doc, "post_id", None),
				comment_id=getattr(doc, "comment_id", None) if interaction_type == IG_INTERACTION_COMMENT_MENTION else None,
			)
			posted = True
		except requests.HTTPError as err:
			flag_if_auth_failure(account.name, err, context=CONTEXT_MENTION_REPLY)
			raise
		except graph.MentionTransient as err:
			raise channels.SendRetry("transient", str(err)) from err
		except graph.MentionUnresolvable as err:
			create_pending(reply_status=IG_CONSENT_REPLY_FAILED, reply_note=str(err))
			return RESULT_SKIPPED_REPLY_REFUSED

		create_pending(reply_status=IG_CONSENT_REPLY_SENT, public_reply_id=reply_id)
		return RESULT_SENT
	except Exception:
		# A posted reply keeps the lock so a retry cannot double-post.
		if not posted:
			channels.release_lock(lock_key)
		raise


def send_ig_consent_template(ctx, step) -> str:
	"""Send Instagram consent generic template message to users who replied via DM.

	Args:
		ctx: Automation execution context containing doc, automation, run, etc.
		step: Automation Step document/dict containing step configuration.

	Returns:
		str: Execution result ("sent", "skipped: ..."). Returns RESULT_SKIPPED_TEMPLATES_DISABLED
			when messenger/instagram templates are disabled for the site.

	Raises:
		ValueError: If doc is not a Social Mention.
		channels.SendRetry: If the duplicate-send guard (Redis) is unavailable.

	Side Effects:
		Acquires a duplicate-send lock (kept after a send, released on failure).
		Sends generic template message via Instagram DM service. May transition
		Instagram Consent Response records to Failed. None when skipped.
	"""
	doc = ctx.doc
	_require_mention(doc, SEND_IG_CONSENT_TEMPLATE)

	if not messenger_templates.is_templates_enabled():
		return RESULT_SKIPPED_TEMPLATES_DISABLED

	consent_names = frappe.get_all(
		IG_CONSENT_DOCTYPE,
		filters={"social_mention": doc.name, "status": IG_CONSENT_DM_RECEIVED},
		pluck="name",
		order_by="creation asc",
	)
	if not consent_names:
		return RESULT_SKIPPED_NO_CONSENT_RECORD

	cfg = {
		key: g(step, field) or ""
		for key, field in (
			("title", "ig_template_title"),
			("subtitle", "ig_template_subtitle"),
			("yes_label", "ig_yes_label"),
			("no_label", "ig_no_label"),
			("view_label", "ig_view_label"),
			("image_source", "ig_image_source"),
		)
	}

	lock_key = f"ig_consent_card:{doc.name}"
	try:
		if not channels.acquire_lock(lock_key):
			return RESULT_SKIPPED_DUPLICATE
	except channels.LockUnavailable as e:
		raise channels.SendRetry("transient", f"duplicate-send guard unavailable: {e}") from e

	try:
		res = ig_consent.send_consent_template(consent_names, cfg)
	except Exception as err:
		channels.release_lock(lock_key)
		reason = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
		ig_consent.mark_failed(consent_names, reason)
		return f"{RESULT_SKIPPED_PREFIX}{reason}"

	if res.get("ok"):
		return RESULT_SENT
	# Nothing was sent: free the lock so a retry is not swallowed as a duplicate.
	channels.release_lock(lock_key)
	reason = res.get("reason") or REASON_SEND_FAILED
	return f"{RESULT_SKIPPED_PREFIX}{reason}"


def execute(ctx, step) -> str:
	"""Run one non-control step; returns a short result string. Raises on error."""
	a, doc = ctx.automation, ctx.doc
	if step.step_type == SEND_WHATSAPP:
		if channels.already_sent(step.template, doc.doctype, doc.name, a.send_frequency, ignore_failed=bool(ctx.get("retrying"))):
			return "skipped: already sent"
		if _is_marketing_opted_out(ctx, step):
			return "skipped: opted out"
		if step.delivery_mode == "Bulk campaign":
			phone = channels.resolve_phone(a, doc)
			if not phone:
				return "skipped: no recipient"
			res = channels.dispatch_bulk(a, step, doc, phone, channels.bulk_body_params(a, step, doc))
			return "sent" if res.get("status") == "success" else f"error: {res.get('reason')}"
		return _send(ctx, step, "wa", lambda: channels.send_whatsapp(a, step, doc, phone=ctx.get("phone")))
	if step.step_type == SEND_EMAIL:
		return _send(ctx, step, "email", lambda: channels.send_email(a, step, doc))
	if step.step_type == SEND_SMS:
		return _send(ctx, step, "sms", lambda: channels.send_sms(a, step, doc))
	if step.step_type == UPDATE_FIELD:
		value = update_value(doc, step)
		frappe.db.set_value(doc.doctype, doc.name, step.update_field, value)
		set_in_memory(doc, step.update_field, value)
		return "updated"
	if step.step_type == ADD_TO_LIST:
		phone = channels.resolve_phone(a, doc)
		if not phone:
			return "skipped: no recipient"
		return "added" if channels.add_to_list(step.recipient_list, phone, doc) else "skipped: already in list"
	if step.step_type == SEND_IG_PUBLIC_REPLY:
		return send_ig_public_reply(ctx, step)
	if step.step_type == SEND_IG_CONSENT_TEMPLATE:
		return send_ig_consent_template(ctx, step)
	return f"skipped: unknown step type {step.step_type}"
