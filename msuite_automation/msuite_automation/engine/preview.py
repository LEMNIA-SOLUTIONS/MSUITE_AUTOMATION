"""Dry run for the builder's Test button: what an automation would do for one
record. Builds every message with the real channel code but never sends, and
runs inside a savepoint that is always rolled back, so nothing is written."""

from __future__ import annotations

import itertools
import re

import frappe
from frappe.core.doctype.server_script.server_script_utils import EVENT_MAP

from msuite_core.constants import IG_CONSENT_POST_TYPE_DEFAULT, IG_CONSENT_POST_TYPE_MAP
from msuite_automation.msuite_automation.constants import IG_CONSENT_DEFAULT_EXPIRY_DAYS
from msuite_automation.msuite_automation.constants import (
	ADD_TO_LIST,
	ANY_SAVE_EVENT_METHODS,
	CANCEL_EVENT_METHODS,
	DELETE_EVENT_METHODS,
	DOCSTATUS_CANCELLED,
	DOCSTATUS_SUBMITTED,
	DOCUMENT_EVENT,
	IF_ELSE,
	PREVIEW_ERROR_MAX,
	SEND_EMAIL,
	SEND_IG_CONSENT_TEMPLATE,
	SEND_IG_PUBLIC_REPLY,
	SEND_SMS,
	SEND_WHATSAPP,
	SCHEDULE,
	SUBMIT_EVENT_METHODS,
	SUBMITTED_SAVE_EVENT_METHODS,
	SWITCH,
	UPDATE_FIELD,
	WAIT,
	WAIT_EVENT,
)
from msuite_automation.msuite_automation.engine import channels, steps as S
from msuite_automation.msuite_automation.engine.executor import _already_ran, _passes, effective_automation, switch_handle, trigger_row
from msuite_automation.msuite_automation.engine.filters import field_value, rule_ok
from msuite_automation.msuite_automation.engine.graph import Graph
from msuite_automation.msuite_automation.engine.triggers import TRIGGER_EVENT_MAP
from msuite_automation.msuite_automation.engine.walker import advance, start_paths

SAVEPOINT = "automation_dry_run"


def dry_run(automation, doc, trigger_id=None) -> dict:
	frappe.db.savepoint(SAVEPOINT)
	try:
		return _dry_run(automation, doc, trigger_id)
	finally:
		frappe.db.rollback(save_point=SAVEPOINT)


def _dry_run(automation, doc, trigger_id):
	trigger = trigger_row(automation, trigger_id, doc.get("doctype"))
	ref = doc.get("name")
	out = {"trigger_id": trigger.trigger_id if trigger else trigger_id,
		"trigger": _trigger_check(trigger, doc, automation),
		"filters": _filters(automation, trigger, doc),
		"already_ran": bool(automation.name and ref) and _already_ran(automation.name, automation.send_frequency, doc.get("doctype"), ref),
		"steps": [], "paths": []}
	if trigger is None:
		return out
	graph = Graph(automation.triggers, automation.steps, automation.edges)
	ctx = frappe._dict(doc=doc)

	def execute(path, node_id):
		step = graph.steps[node_id]
		row = {"path_id": path["path_id"], "node_id": node_id, "step_id": node_id, "step_type": step.step_type}
		if path.get("joined"):
			row["joined"] = True
		out["steps"].append(row)
		if path["path_id"] not in out["paths"]:
			out["paths"].append(path["path_id"])
		try:
			if step.step_type == IF_ELSE:
				row["branch_taken"] = S.branch_of(ctx, step)
				return row["branch_taken"].lower()
			if step.step_type == SWITCH:
				row["case"] = switch_handle(step, doc)
				return row["case"]
			if step.step_type == WAIT:
				row["wait"] = f"{step.wait_value} {step.wait_unit}"
				return ""  # a dry run looks past waits
			if step.step_type == WAIT_EVENT:
				check = step.get("check_doctype") or "event"
				timeout = f"{step.wait_value} {step.wait_unit}"
				row["description"] = f"Wait for {check} (timeout {timeout})"
				row["wait"] = timeout
				return "event"
			row.update(_preview(automation, step, doc))
		except Exception as e:
			row["error"] = str(e)[:PREVIEW_ERROR_MAX]
			if step.step_type == IF_ELSE:
				row["branch_taken"] = "No"
				return "no"
		return ""

	counter = itertools.count(2)
	paths = advance(graph, start_paths(trigger.trigger_id), execute, lambda: f"p{next(counter)}")
	for p in paths:
		if p.get("error"):
			out["steps"].append({
				"path_id": p["path_id"],
				"node_id": p["node_id"],
				"step_id": p["node_id"],
				"error": p["error"],
			})
	return out


def _trigger_check(trigger, doc, automation) -> dict:
	"""Check if the trigger would fire for the document in dry-run preview.

	Args:
		trigger: The Automation Trigger row/dict.
		doc: The target document or dict.
		automation: The automation; needed to evaluate Schedule filters.

	Returns:
		dict: {"matches": bool | None, "reason": str}

	Raises:
		None.

	Side Effects:
		None.
	"""
	if not trigger:
		return {"matches": None, "reason": "No trigger configured"}

	tt = trigger.trigger_type
	if tt == "Field Change":
		from msuite_automation.msuite_automation.engine.triggers import normalize
		field = trigger.field
		current_raw = doc.get(field) if hasattr(doc, "get") else getattr(doc, field, None)
		current_norm = normalize(current_raw)
		to_norm = normalize(trigger.to_value)
		note_from = " (from_value cannot be verified from current record state)" if trigger.from_value else ""

		if trigger.to_value:
			if current_norm.lower() == to_norm.lower():
				return {"matches": True, "reason": f"Field '{field}' currently matches '{trigger.to_value}'{note_from}"}
			return {"matches": False, "reason": f"Field '{field}' currently '{current_raw or 'blank'}' does not match '{trigger.to_value}'{note_from}"}
		else:
			if current_norm:
				return {"matches": True, "reason": f"Field '{field}' is currently set to '{current_raw}'{note_from}"}
			return {"matches": False, "reason": f"Field '{field}' is currently empty{note_from}"}

	if tt == "Date Based":
		from frappe.utils import add_to_date, nowdate
		date_field = trigger.date_field
		val = doc.get(date_field) if hasattr(doc, "get") else getattr(doc, date_field, None)
		if not val:
			return {"matches": False, "reason": f"Date field '{date_field}' is currently empty"}
		days = int(trigger.offset_days or 0) * (1 if trigger.offset_direction == "Before" else -1)
		target_day = str(add_to_date(nowdate(), days=days))
		val_day = str(val)[:10]
		if val_day == target_day:
			return {"matches": True, "reason": f"Date field '{date_field}' ({val_day}) matches target date ({target_day})"}
		return {"matches": False, "reason": f"Date field '{date_field}' ({val_day}) does not match target date ({target_day})"}

	if tt == DOCUMENT_EVENT:
		return _document_event_check(trigger, doc)

	if tt == SCHEDULE:
		freq = trigger.schedule_frequency
		try:
			passes = _passes(automation, trigger, doc)
		except Exception as e:
			return {"matches": False, "reason": f"{freq} schedule: filter check failed: {str(e)[:PREVIEW_ERROR_MAX]}"}
		if passes:
			return {"matches": True, "reason": f"{freq} schedule: record passes the trigger filters"}
		return {"matches": False, "reason": f"{freq} schedule: record does not pass the trigger filters"}

	return {"matches": None, "reason": "not evaluated"}


def _document_event_check(trigger, doc) -> dict:
	"""Whether the record's current state would fire this Document Event trigger.

	Args:
		trigger: Automation Trigger row with `doc_event` (a TRIGGER_EVENT_MAP label).
		doc: The target document or dict; a missing docstatus counts as 0 (draft).

	Returns:
		dict: {"matches": bool | None, "reason": str}; None for delete events.

	Raises:
		None.

	Side Effects:
		None.
	"""
	def labels(methods):
		# on_trash is "Before Delete" in EVENT_MAP but "On Trash" in TRIGGER_EVENT_MAP; accept both.
		return {m_map[m] for m in methods for m_map in (EVENT_MAP, TRIGGER_EVENT_MAP)}

	event = trigger.doc_event
	docstatus = int(doc.get("docstatus") or 0)
	if event in labels(DELETE_EVENT_METHODS):
		return {"matches": None, "reason": "fires when the record is deleted"}
	for methods, needed in ((SUBMIT_EVENT_METHODS, DOCSTATUS_SUBMITTED), (CANCEL_EVENT_METHODS, DOCSTATUS_CANCELLED)):
		if event in labels(methods):
			ok = docstatus == needed
			return {"matches": ok, "reason": f"'{event}' needs docstatus {needed}; record has {docstatus}"}
	if event in labels(SUBMITTED_SAVE_EVENT_METHODS):
		ok = docstatus == DOCSTATUS_SUBMITTED
		return {"matches": ok, "reason": f"'{event}' needs a submitted record (docstatus {DOCSTATUS_SUBMITTED}); record has {docstatus}"}
	if event in labels(ANY_SAVE_EVENT_METHODS):
		return {"matches": True, "reason": f"'{event}' fires on any saved record"}
	return {"matches": None, "reason": f"'{event}' cannot be checked from the current record"}


def _filters(automation, trigger, doc) -> dict:
	rules = [f.as_dict() for f in (automation.filters or []) if trigger is not None and f.trigger_id in ("", None, trigger.trigger_id)]
	try:
		passed, error = (_passes(automation, trigger, doc) if trigger is not None else False), ""
	except Exception as e:
		passed, error = False, str(e)[:PREVIEW_ERROR_MAX]
	rows = []
	for r in rules:
		if not r.get("field"):
			continue
		actual, op = field_value(doc, r["field"]), r.get("operator") or "="
		rows.append({"field": r["field"], "operator": op, "value": r.get("value"),
			"actual": "" if actual is None else str(actual), "ok": rule_ok(actual, op, r.get("value"))})
	return {"passed": passed, "logic": (trigger.filter_logic if trigger is not None else None) or "All", "rules": rows,
		"has_condition": bool(trigger is not None and trigger.condition and trigger.trigger_type != "Schedule"),
		"is_return_skipped": bool(automation.skip_return_documents and doc.get("is_return") in (1, "1", True)),
		"error": error}


def _fill(text, values):
	"""values: {"1": "Asha"} for positional templates, {"name": "Asha"} for NAMED ones."""
	return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), text or "")


def _render_or_error(text: str, ctx: dict) -> str:
	"""Render a Jinja string for the preview; a template error is shown inline, not raised.

	Args:
		text: Template source (may be empty).
		ctx: Jinja context matching what the real step renders with.

	Returns:
		Rendered text, "" for empty input, or "[Invalid template: <error>]".

	Raises:
		None.

	Side Effects:
		None.
	"""
	if not text:
		return ""
	try:
		return frappe.render_template(text, ctx)
	except Exception as err:
		return f"[Invalid template: {err}]"


def _preview(automation, step, doc) -> dict:
	eff = effective_automation(automation, step, doc.get("doctype"))
	t = step.step_type
	if t == SEND_WHATSAPP:
		ctx = frappe._dict(automation=eff, doc=doc)
		if S._is_marketing_opted_out(ctx, step):
			return {"skipped": "opted out"}
		if step.delivery_mode == "Bulk campaign":
			to = channels.resolve_phone(eff, doc)
			return {"bulk": True, "to": to} if to else {"skipped": "no recipient"}
		msg = channels.build_whatsapp(eff, step, doc)
		if not msg:
			return {"skipped": "no recipient"}
		body = [p for c in msg["payload"]["components"] if c.get("type") == "body" for p in c.get("parameters", [])]
		params = [p.get("text", "") for p in body]
		values = {p.get("parameter_name") or str(i + 1): p.get("text", "") for i, p in enumerate(body)}
		return {"to": msg["to"], "template": msg["template"], "params": params,
			"text": _fill(getattr(channels._template(msg["template"]), "template", ""), values)}
	if t == SEND_EMAIL:
		return channels.render_email(eff, step, doc) or {"skipped": "no recipient"}
	if t == SEND_SMS:
		return channels.render_sms(eff, step, doc) or {"skipped": "no recipient"}
	if t == UPDATE_FIELD:
		field = step.update_field
		current = doc.get(field)
		value = S.update_value(doc, step)
		S.set_in_memory(doc, field, value)
		return {"field": field, "from": "" if current is None else str(current), "to": "" if value is None else str(value)}
	if t == ADD_TO_LIST:
		to = channels.resolve_phone(eff, doc)
		return {"list": step.recipient_list, "to": to} if to else {"skipped": "no recipient"}
	if t == SEND_IG_PUBLIC_REPLY:
		raw_text = step.get("ig_reply_text") or ""
		# Same context as steps.send_ig_public_reply.
		reply_ctx = {"username": doc.get("username") or "", "doc": doc.as_dict()}
		rendered = _render_or_error(raw_text, reply_ctx)
		return {
			"reply_text": rendered,
			"description": f"Public reply: {rendered}" if rendered else "Public reply",
			"expiry_days": step.get("ig_expiry_days") or IG_CONSENT_DEFAULT_EXPIRY_DAYS,
		}
	if t == SEND_IG_CONSENT_TEMPLATE:
		interaction_type = doc.get("interaction_type") or ""
		# Same context as ig_consent.build_consent_elements: only these two keys.
		ctx = {
			"username": doc.get("username") or "",
			"post_type": IG_CONSENT_POST_TYPE_MAP.get(interaction_type, IG_CONSENT_POST_TYPE_DEFAULT),
		}
		rendered_title = _render_or_error(step.get("ig_template_title") or "", ctx)
		return {
			"title": rendered_title,
			"subtitle": _render_or_error(step.get("ig_template_subtitle") or "", ctx),
			"description": f"Send IG Consent Template: {rendered_title}" if rendered_title else "Send IG Consent Template",
			"yes_label": _render_or_error(step.get("ig_yes_label") or "", ctx),
			"no_label": _render_or_error(step.get("ig_no_label") or "", ctx),
			"view_label": _render_or_error(step.get("ig_view_label") or "", ctx),
			"image_source": step.get("ig_image_source") or "",
		}
	return {"skipped": f"unknown step type {t}"}
