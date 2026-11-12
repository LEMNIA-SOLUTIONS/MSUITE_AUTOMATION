"""Channel senders for automation steps. Bodies moved from the legacy
notification_service / lead_status_automation with parameters re-pointed at
(automation, step, doc) — message building is unchanged."""

from __future__ import annotations

import datetime
import json
import re

import frappe
from frappe.desk.form.utils import get_pdf_link
from frappe.model.document import Document
from frappe.utils import cint, today

from msuite_core.utils.phone import format_number
from msuite_inbox.inbox.services.outbound import send_to_phone
from msuite_automation.msuite_automation.constants import DEDUPE_LOCK_TTL_SECONDS, ONCE_PER_DAY, ONCE_PER_DOCUMENT
from msuite_automation.msuite_automation.engine.filters import ALIASES, _get, field_value, get_linked_sales_order
from msuite_core.constants import LOGGER_NAME
from msuite_core.utils.phone import clean_mobile

PHONE_FALLBACK = ("contact_mobile", "mobile_no", "whatsapp_no", "cell_number", "contact_phone", "phone")
EMAIL_FALLBACK = ("email_id", "email", "contact_email")


def channel_app(channel: str):
	"""The module a channel's app registers for automation sends, or None if it is not on this site."""
	paths = frappe.get_hooks("msuite_automation_steps", default={}).get(channel)
	return frappe.get_module(paths[-1]) if paths else None


def _require_channel_app(channel: str):
	module = channel_app(channel)
	if not module:
		raise RuntimeError(f"The {channel} app is not installed on this site")
	return module


def claim_for_account(account: str | None, recipient: str) -> bool:
	return _require_channel_app("WhatsApp").claim_send_slot(account, recipient)


def whatsapp_opted_out(phone: str) -> bool:
	"""The WhatsApp opt-out registry. A site without the app has nothing to send, so nothing to block."""
	module = channel_app("WhatsApp")
	return module.is_opted_out(phone) if module else False


class RecipientOptedOut(Exception):
	"""Raised by send_* when the recipient unsubscribed from this channel."""


class SendRetry(RuntimeError):
	"""A send that failed in a way that is safe to repeat later (never accepted by Meta)."""

	def __init__(self, kind: str, message: str):
		super().__init__(message)
		self.kind = kind


_OPT_OUT_FIELD = {"WhatsApp": "unsubscribed_whatsapp", "SMS": "unsubscribed_sms"}


def is_opted_out(channel: str, address) -> bool:
	"""Same consent registries the bulk senders honour (audit H6):
	Audience Member per-channel flags for phones, the global email
	suppression registry (+ unsubscribed Email Recipient rows) for email."""
	if not address:
		return False
	if channel == "Email":
		email_app = channel_app("Email")
		email = str(address).strip().lower()
		# A reply or unsubscribe from an address outside every Audience only flags the Contact.
		return (
			(email_app.is_suppressed(email) if email_app else False)
			or bool(frappe.db.exists("Email Recipient", {"email_address": email, "unsubscribed": 1}))
			or any(
				frappe.db.exists("Contact", {col: email, flag: 1})
				for col in ("email_normalized", "email_id")
				for flag in ("unsubscribed_email", "unsubscribed")
			)
		)
	if channel == "WhatsApp":
		from msuite_core.utils.phone import is_bsuid
		clean = str(address).strip()
		if is_bsuid(clean):
			import json
			handle = frappe.db.get_value(
				"Channel Handle",
				{"channel_type": "WhatsApp", "bsuid": clean},
				["name", "metadata"],
				as_dict=True,
			) or frappe.db.get_value(
				"Channel Handle",
				{"channel_type": "WhatsApp", "platform_handle": clean},
				["name", "metadata"],
				as_dict=True,
			)
			if handle and handle.metadata:
				meta = json.loads(handle.metadata) if isinstance(handle.metadata, str) else dict(handle.metadata or {})
				return bool(meta.get("opted_out"))
			return False

	digits = re.sub(r"\D", "", str(address))
	if not digits:
		return False
	variants = [f"+{digits}", digits]
	flag = _OPT_OUT_FIELD[channel]
	# STOP from a number outside every Audience only flags the Contact, so check both stores.
	return bool(
		frappe.db.exists("Audience Member", {"phone_e164": ["in", variants], flag: 1})
	) or any(
		frappe.db.exists("Contact", {col: v, flag: 1})
		for col in ("phone_e164", "mobile_no")
		for v in variants
	)


def _first(doc, fields):
	for f in fields:
		v = field_value(doc, f) if f else None
		if v is not None and str(v).strip():
			return str(v).strip()
	return None


def _doc_data(doc) -> dict:
	if not doc:
		return {}
	if callable(getattr(doc, "as_dict", None)):
		return doc.as_dict()
	return dict(doc) if hasattr(doc, "items") or isinstance(doc, dict) else {}


def _formatted_doc_value(doc, fname: str):
	if not fname:
		return None
	target, f = (get_linked_sales_order(doc), fname[len("sales_order."):]) if fname.startswith("sales_order.") else (doc, fname)
	if target and isinstance(target, Document) and hasattr(target, "meta") and target.meta.has_field(f):
		val = target.get_formatted(f)
		if (val is None or str(val).strip() == "") and f in ALIASES and target.meta.has_field(ALIASES[f]):
			val = target.get_formatted(ALIASES[f])
		return val
	return field_value(doc, fname) if target or not fname.startswith("sales_order.") else None


def _render_context(doc, doc_data: dict) -> dict:
	ctx = {"doc": doc}
	if doc_data.get("doctype") == "Delivery Note":
		ctx["sales_order"] = get_linked_sales_order(doc)
	return ctx


def _lead_display_name(doc, fallback_to_name: bool = False) -> str:
	name = _get(doc, "lead_name")
	if not name:
		first = _get(doc, "first_name", "")
		last = _get(doc, "last_name", "")
		name = f"{first} {last}".strip()
	if not name and fallback_to_name:
		name = _get(doc, "name", "")
	return name or ""


def _lead_recipient_data(doc) -> dict:
	return {
		"lead_id": _get(doc, "name", ""),
		"email": _get(doc, "email", "") or "",
		"organization": _get(doc, "organization", "") or "",
	}


def resolve_phone(automation, doc):
	raw = _first(doc, [_get(automation, "phone_field"), *PHONE_FALLBACK])
	if not raw:
		return None
	cleaned, error = clean_mobile(raw)
	return format_number(cleaned if cleaned and not error else raw).replace(" ", "")


def resolve_email(automation, doc):
	return _first(doc, [_get(automation, "email_field"), *EMAIL_FALLBACK])


def _template(name):
	return frappe.get_doc("WhatsApp Templates", name)


def _mappings_for(automation, step_id):
	return [m for m in (_get(automation, "variable_mappings") or []) if _get(m, "step_id") == step_id]


def already_sent(template, doctype, name, frequency, ignore_failed: bool = False) -> bool:
	if frequency not in (ONCE_PER_DAY, ONCE_PER_DOCUMENT) or not template:
		return False
	flt = {
		"reference_doctype": doctype,
		"reference_name": str(name) if name is not None else name,
		"template_name": template,
		"direction": "Outbound",
	}
	if frequency == ONCE_PER_DAY:
		flt["creation"] = (">=", today() + " 00:00:00")
	if frequency == ONCE_PER_DOCUMENT or ignore_failed:
		flt["status"] = ("in", ("queued", "sent", "delivered", "read"))
	return bool(frappe.db.exists("Channel Message", flt))


class LockUnavailable(RuntimeError):
	"""Redis could not be reached, so it is unknown whether the lock is free."""


def acquire_lock(key: str, ttl: int = DEDUPE_LOCK_TTL_SECONDS, expires_in_sec: int | None = None) -> bool:
	"""Atomic acquire BEFORE sending. True: acquired (send may proceed). False: already held.
	Raises LockUnavailable when Redis fails: the caller must not assume the lock is free.
	"""
	cache = frappe.cache()
	try:
		return bool(cache.set(cache.make_key(key), "1", nx=True, ex=expires_in_sec or ttl))
	except Exception as e:
		raise LockUnavailable(str(e)) from e


def release_lock(key: str) -> None:
	"""Release the lock when a send fails so the retry is not swallowed as a duplicate."""
	cache = frappe.cache()
	try:
		cache.delete(cache.make_key(key))
	except Exception as e:
		frappe.logger(LOGGER_NAME).warning(f"release_lock failed for {key}; a retry may be skipped as duplicate until the lock expires: {e}")


def build_whatsapp(automation, step, doc, phone=None) -> dict | None:
	phone_number = phone or resolve_phone(automation, doc)
	doc_data = _doc_data(doc)

	if not phone_number or str(phone_number).strip() == "":
		return None

	template_name = _get(step, "template")
	if not template_name:
		raise ValueError("WhatsApp template is required")

	template = _template(template_name)
	if not template:
		raise ValueError(f"WhatsApp template '{template_name}' not found")

	data = {
		"messaging_product": "whatsapp",
		"to": format_number(phone_number),
		"type": "template",
		"template": {
			"name": template_name,
			"language": {"code": getattr(template, "language_code", None) or "en"},
			"components": [],
		},
	}

	is_named = getattr(template, "parameter_format", None) == "NAMED"
	var_names = re.findall(r"\{\{(\w+)\}\}", getattr(template, "template", "") or "") if is_named else []
	step_id = _get(step, "step_id")
	mappings = _mappings_for(automation, step_id)

	if mappings:
		parameters = []
		for idx, m in enumerate(mappings):
			pname = var_names[idx] if is_named and idx < len(var_names) else ""
			vkey = _get(m, "variable_key")
			param_name = vkey if is_named and vkey and not str(vkey).isdigit() else pname
			fname = _get(m, "source_field", "")
			fallback = _get(m, "static_fallback", "")

			value = _formatted_doc_value(doc, fname)
			if value is None or str(value).strip() == "":
				value = fallback or (param_name.replace("_", " ").title() if param_name else "-")

			if isinstance(value, (datetime.date, datetime.datetime)):
				value = str(value)

			param_entry = {"type": "text", "text": str(value)}
			if is_named and param_name:
				param_entry["parameter_name"] = param_name
			parameters.append(param_entry)

		data["template"]["components"].append({"type": "body", "parameters": parameters})

	url = filename = None
	header_source = _get(step, "header_source")
	if header_source == "Document Print":
		key = doc.get_document_share_key() if callable(getattr(doc, "get_document_share_key", None)) else ""
		meta = frappe.get_meta(doc_data.get("doctype")) if doc_data.get("doctype") else None
		print_format = (
			_get(step, "print_format")
			or getattr(meta, "default_print_format", None)
			or "Standard"
		)
		link = get_pdf_link(doc_data.get("doctype"), doc_data.get("name"), print_format=print_format)
		filename = f"{doc_data.get('name')}.pdf"
		url = f"{frappe.utils.get_url()}{link}&key={key}"

	elif header_source in ("File", "Field"):
		file_url = _get(step, "header_file") if header_source == "File" else doc_data.get(_get(step, "header_field"))
		if file_url and isinstance(file_url, str):
			if header_source == "Field" and not file_url.startswith("http"):
				key = doc.get_document_share_key() if callable(getattr(doc, "get_document_share_key", None)) else ""
				file_url = f"{frappe.utils.get_url()}{file_url}&key={key}"
			url = file_url if file_url.startswith("http") else f"{frappe.utils.get_url()}{file_url}"
			filename = file_url.split("/")[-1].split("?")[0] or "document.pdf"

	tpl_header_type = getattr(template, "header_type", None)
	if tpl_header_type in ("DOCUMENT", "IMAGE"):
		if not url and getattr(template, "media_sample", None):
			sample_path = template.media_sample
			url = sample_path if sample_path.startswith("http") else f"{frappe.utils.get_url()}{sample_path}"
			if tpl_header_type == "DOCUMENT":
				filename = filename or f"{getattr(template, 'template_name', None) or getattr(template, 'name', 'document')}.pdf"

		if url:
			param = {"type": "document", "document": {"link": url, "filename": filename or "document.pdf"}} if tpl_header_type == "DOCUMENT" else {"type": "image", "image": {"link": url}}
			data["template"]["components"].append({"type": "header", "parameters": [param]})

	buttons = getattr(template, "buttons", None) or []
	if buttons:
		btn_flds = _get(step, "button_fields")
		button_fields = [f.strip() for f in btn_flds.split(",") if f.strip()] if isinstance(btn_flds, str) else list(btn_flds or [])
		for idx, btn in enumerate(buttons):
			if not button_fields:
				break
			btype = getattr(btn, "button_type", None) or (btn.get("button_type") if isinstance(btn, dict) else None)
			utype = getattr(btn, "url_type", None) or (btn.get("url_type") if isinstance(btn, dict) else None)
			sub_type, param = None, None
			if btype == "Visit Website" and utype == "Dynamic":
				bf = button_fields.pop(0)
				sub_type = "url"
				param = {"type": "text", "text": str(field_value(doc, bf) or "")}
			elif btype in ("Multi-Product Message", "Catalog"):
				bf = button_fields.pop(0)
				sub_type = "mpm" if btype == "Multi-Product Message" else "catalog"
				param = {"type": "action", "action": field_value(doc, bf)}
			if sub_type:
				data["template"]["components"].append(
					{
						"type": "button",
						"sub_type": sub_type,
						"index": str(idx),
						"parameters": [param],
					}
				)

	payload = {
		"type": "template",
		"template": template_name,
		"language": getattr(template, "language_code", None) or "en",
		"components": data["template"]["components"],
		"sender_type": "Bot",
	}

	if _get(automation, "whatsapp_phone"):
		payload["phone_number_id"] = _get(automation, "whatsapp_phone")

	return {"to": data.get("to"), "template": template_name, "payload": payload,
		"account": _get(automation, "whatsapp_account") or None}


def send_whatsapp(automation, step, doc, phone=None) -> str | bool:
	"""Send a WhatsApp message via outbound service.

	Args:
		automation: The Automation document.
		step: Step definition dict or object.
		doc: Trigger document.
		phone: Optional override recipient phone number.

	Returns:
		str | bool: Channel Message name if available, otherwise True on success; False if no phone.

	Raises:
		RecipientOptedOut: If recipient opted out of marketing templates.
		SendRetry: If messaging limit reached or retryable error occurs.
		RuntimeError: If WhatsApp send fails permanently.

	Side Effects:
		Dispatches WhatsApp message via inbox outbound service.
	"""
	msg = build_whatsapp(automation, step, doc, phone)
	doc_data = _doc_data(doc)
	if not msg:
		frappe.logger(LOGGER_NAME).warning(
			f"Workflow {_get(automation, 'name', 'Automation')}: no phone number for WhatsApp send on {doc_data.get('doctype')} {doc_data.get('name')}, skipping"
		)
		return False
	from msuite_automation.msuite_automation.engine.steps import _is_marketing_opted_out

	ctx = frappe._dict(automation=automation, doc=doc, phone=phone or msg.get("to"))
	if _is_marketing_opted_out(ctx, step):
		raise RecipientOptedOut(msg["to"])
	if not claim_for_account(msg["account"], msg["to"]):
		raise SendRetry("limit", "WhatsApp portfolio messaging limit reached; will retry")
	res = send_to_phone(
		phone=msg["to"],
		payload=msg["payload"],
		whatsapp_account=msg["account"],
		reference_doctype=doc_data.get("doctype"),
		reference_name=doc_data.get("name"),
	)
	if isinstance(res, dict) and not res.get("ok", True):
		if res.get("retry"):
			raise SendRetry(res["retry"], res.get("error") or "WhatsApp send failed")
		raise RuntimeError(res.get("error") or "WhatsApp send failed")
	if isinstance(res, dict) and res.get("message"):
		return res["message"]
	return True


def render_email(automation, step, doc) -> dict | None:
	recipient = resolve_email(automation, doc)
	if not recipient:
		return None
	context = _render_context(doc, _doc_data(doc))
	return {"to": recipient,
		"subject": frappe.render_template(_get(step, "email_subject", "") or "", context),
		"message": frappe.render_template(_get(step, "email_body", "") or "", context)}


def send_email(automation, step, doc) -> bool:
	doc_data = _doc_data(doc)
	msg = render_email(automation, step, doc)
	if not msg:
		frappe.logger(LOGGER_NAME).warning(
			f"Workflow {_get(automation, 'name', 'Automation')}: no email in field '{_get(automation, 'email_field')}' on {doc_data.get('doctype')} {doc_data.get('name')}, skipping email send"
		)
		return False
	if is_opted_out("Email", msg["to"]):
		raise RecipientOptedOut(msg["to"])
	kwargs = {
		"recipients": [msg["to"]],
		"subject": msg["subject"],
		"message": msg["message"],
		"reference_doctype": doc_data.get("doctype"),
		"reference_name": doc_data.get("name"),
		"now": False,
	}
	if _get(step, "attach_print"):
		kwargs["attachments"] = [frappe.attach_print(doc_data.get("doctype"), doc_data.get("name"), print_format=_get(step, "print_format") or None)]
	frappe.sendmail(**kwargs)
	return True


def render_sms(automation, step, doc) -> dict | None:
	phone_number = resolve_phone(automation, doc)
	if not phone_number:
		return None
	context = _render_context(doc, _doc_data(doc))
	return {"to": format_number(phone_number), "message": frappe.render_template(_get(step, "sms_message", "") or "", context)}


def send_sms(automation, step, doc) -> bool:
	auto_name = _get(automation, "name", "Automation")
	msg = render_sms(automation, step, doc)
	if not msg:
		frappe.logger(LOGGER_NAME).warning(f"Workflow {auto_name}: no phone number for SMS send, skipping")
		return False
	if is_opted_out("SMS", msg["to"]):
		raise RecipientOptedOut(msg["to"])
	result = _require_channel_app("SMS").send_single(_get(automation, "sms_account"), msg["to"], msg["message"])
	if not getattr(result, "ok", False):
		frappe.log_error(
			title=f"Workflow {auto_name}: SMS send failed",
			message=getattr(result, "error", "") or "SMS provider reported failure",
		)
		return False
	return True


def add_to_list(list_name: str, phone: str, doc) -> bool:
	"""Add recipient to list if not already present. Returns True if newly added, False if existed."""
	if frappe.db.exists("WhatsApp Recipient", {"parent": list_name, "mobile_number": phone}):
		return False

	display_name = _lead_display_name(doc)
	try:
		list_doc = frappe.get_doc("WhatsApp Recipient List", list_name)
		list_doc.append(
			"recipients",
			{
				"mobile_number": phone,
				"recipient_name": display_name or "",
				"recipient_data": json.dumps(_lead_recipient_data(doc)),
			},
		)
		list_doc.save(ignore_permissions=True)
		return True
	except Exception as e:
		frappe.logger(LOGGER_NAME).warning(
			f"[Lead Status Automation] Could not append recipient {phone} to list {list_name}: {e}"
		)
		return False


def bulk_body_params(automation, step, doc) -> dict:
	step_id = _get(step, "step_id")
	params = {}
	for m in _mappings_for(automation, step_id):
		vkey = _get(m, "variable_key", "")
		sfield = _get(m, "source_field", "")
		fallback = _get(m, "static_fallback", "")
		params[vkey] = str(field_value(doc, sfield) or fallback or "")
	return params


def dispatch_bulk(automation, step, doc, phone: str, body_params: dict) -> dict:
	"""Aggregate or trigger Bulk WhatsApp Message broadcast for automation rule."""
	auto_name = _get(automation, "name")
	auto_title = _get(automation, "title")
	wa_account = _get(automation, "whatsapp_account")
	trigger_label = _get(automation, "_trigger_label", "Status") or "Status"

	recipient_list_name = _get(step, "recipient_list")
	target_bulk = _get(step, "target_bulk_message")
	campaign = _get(step, "bulk_campaign")
	schedule_type = _get(step, "bulk_schedule_type") or "Immediate"
	scheduled_time = _get(step, "bulk_scheduled_time")
	delay_mins = cint(_get(step, "bulk_delay_minutes", 0))
	use_batched = cint(_get(step, "use_batched_enqueue", 1))
	template_name = _get(step, "template")
	rule_name = auto_title or auto_name

	# 1. If explicit target broadcast is selected, route to that broadcast
	if target_bulk and frappe.db.exists("Bulk WhatsApp Message", target_bulk):
		target_doc = frappe.get_doc("Bulk WhatsApp Message", target_bulk)
		target_list = target_doc.recipient_list or recipient_list_name
		display_name = _lead_display_name(doc, fallback_to_name=True)

		if target_doc.recipient_type == "Recipient List" and target_list:
			rec_count = frappe.db.count("WhatsApp Recipient", {"parent": target_list, "parenttype": "WhatsApp Recipient List"})
			target_doc.db_set("recipient_count", rec_count)
		elif target_doc.recipient_type == "Individual":
			# Ensure recipient is present in target_doc's inline recipients table
			exists = any(r.mobile_number == phone for r in (target_doc.recipients or []))
			if not exists:
				target_doc.append(
					"recipients",
					{
						"mobile_number": phone,
						"recipient_name": display_name,
						"recipient_data": json.dumps(_lead_recipient_data(doc)),
					},
				)
				target_doc.recipient_count = len(target_doc.recipients)
				target_doc.save(ignore_permissions=True)
			else:
				target_doc.db_set("recipient_count", len(target_doc.recipients or []))

		target_doc.db_set("is_lead_automation_broadcast", 1)
		target_doc.db_set("lead_automation_rule", auto_name)

		if schedule_type == "Immediate":
			whatsapp_app = _require_channel_app("WhatsApp")

			recipient_info = {
				"mobile_number": phone,
				"recipient_name": display_name,
				"recipient_data": _doc_data(doc),
				"body_param": body_params,
			}

			if target_doc.status in ("Draft", "Scheduled"):
				target_doc.status = "In Progress"
				target_doc.db_set("status", "In Progress")
				whatsapp_app.queue_bulk(target_doc)
			else:
				# Target broadcast was already launched or completed: immediately dispatch to this incoming lead
				whatsapp_app.send_one_bulk(target_doc, recipient_info)

		frappe.logger(LOGGER_NAME).info(f"[Lead Status Automation] Attached lead to target Bulk WhatsApp Message '{target_doc.name}'.")
		return {"status": "success", "bulk_message": target_doc.name, "mode": "target_attached"}

	# 2. Check for existing open broadcast for this recipient list & template
	existing_bulk = frappe.db.get_value(
		"Bulk WhatsApp Message",
		{
			"recipient_list": recipient_list_name,
			"template": template_name,
			"status": ["in", ["Draft", "Scheduled"]],
		},
		["name", "status", "scheduled_time"],
		as_dict=True,
	)

	if existing_bulk and schedule_type != "Immediate":
		count = frappe.db.count("WhatsApp Recipient", {"parent": recipient_list_name, "parenttype": "WhatsApp Recipient List"}) if recipient_list_name else 1
		frappe.db.set_value("Bulk WhatsApp Message", existing_bulk.name, {
			"recipient_count": count,
			"is_lead_automation_broadcast": 1,
			"lead_automation_rule": auto_name,
		})
		return {"status": "success", "bulk_message": existing_bulk.name, "mode": "aggregated"}

	# Calculate scheduled time if applicable
	exec_scheduled_time = None
	initial_status = "Draft"
	if schedule_type == "Scheduled Time" and scheduled_time:
		exec_scheduled_time = scheduled_time
		initial_status = "Scheduled"
	elif schedule_type == "Delay Buffer" and delay_mins > 0:
		from frappe.utils import add_to_date, now_datetime

		exec_scheduled_time = add_to_date(now_datetime(), minutes=delay_mins)
		initial_status = "Scheduled"

	# Build bulk message doc
	bulk_title = f"{rule_name} - {trigger_label} Broadcast"
	rec_count = frappe.db.count("WhatsApp Recipient", {"parent": recipient_list_name, "parenttype": "WhatsApp Recipient List"}) if recipient_list_name else 1

	try:
		bulk_doc = frappe.get_doc(
			{
				"doctype": "Bulk WhatsApp Message",
				"title": bulk_title,
				"campaign": campaign or None,
				"recipient_type": "Recipient List",
				"recipient_list": recipient_list_name,
				"template": template_name,
				"whatsapp_account": wa_account or None,
				"use_batched_enqueue": use_batched,
				"recipient_count": rec_count,
				"status": initial_status,
				"scheduled_time": exec_scheduled_time,
				"is_lead_automation_broadcast": 1,
				"lead_automation_rule": auto_name,
				"template_variables": json.dumps(body_params) if body_params else "{}",
			}
		)
		bulk_doc.insert(ignore_permissions=True)

		if schedule_type == "Immediate":
			bulk_doc.status = "In Progress"
			bulk_doc.db_set("status", "In Progress")
			_require_channel_app("WhatsApp").queue_bulk(bulk_doc)

		frappe.logger(LOGGER_NAME).info(f"[Lead Status Automation] Created Bulk WhatsApp Message '{bulk_doc.name}' for rule '{rule_name}'.")
		return {"status": "success", "bulk_message": bulk_doc.name, "mode": "created"}

	except Exception as e:
		frappe.logger(LOGGER_NAME).error(f"[Lead Status Automation] Failed to create Bulk WhatsApp Message for rule '{rule_name}': {e}", exc_info=True)
		return {"status": "failed", "reason": f"bulk_create_error: {str(e)}"}
