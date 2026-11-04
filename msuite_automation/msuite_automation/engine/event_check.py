"""Wait for Event condition evaluation: same-doc and linked-record modes."""

from __future__ import annotations

import json
import re

import frappe
from frappe.utils import get_datetime

from msuite_automation.msuite_automation.constants import EVENT_CANDIDATE_LIMIT, WAIT_KEY_MAX_LENGTH
from msuite_automation.msuite_automation.engine.filters import field_value, rules_pass
from msuite_core.services.validation import phone as phone_validator


def g(row, key, default=None):
	val = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
	return default if val is None else val


_PHONE_LIKE = re.compile(r"^\+?[\d\s().-]{7,}$")


def _is_phone_field(doctype: str, fieldname: str) -> bool:
	df = frappe.get_meta(doctype).get_field(fieldname)
	if df and (df.fieldtype == "Phone" or df.options == "Phone"):
		return True
	return any(k in fieldname.lower() for k in ("phone", "mobile", "whatsapp"))


def _match_values(value, phone_field: bool = True) -> list[str]:
	"""The raw value, plus its common phone spellings when the searched field holds phones and the
	value looks like one, so an order's `+91 98765 43210` finds a message stored as `919876543210`.
	Anything else (customer IDs, document names) matches exactly."""
	raw = str(value).strip()
	if not raw:
		return []
	values = [raw]
	if phone_field and _PHONE_LIKE.match(raw):
		e164 = phone_validator.normalize(raw).get("e164")
		if e164:
			national = str(phone_validator.phonenumbers.parse(e164).national_number)
			values += [e164, e164.lstrip("+"), national]
	return list(dict.fromkeys(values))


def wait_key(step, doc) -> str:
	if not doc:
		return ""
	check_doctype = g(step, "check_doctype")
	if not check_doctype:
		return ""
	link_field = g(step, "check_link_field") or ""
	match_field = g(step, "check_match_field") or "name"
	val = field_value(doc, match_field)
	if val is None:
		return ""
	raw = str(val).strip()
	if not raw:
		return ""
	if link_field and _is_phone_field(check_doctype, link_field):
		e164 = phone_validator.normalize(raw).get("e164") if _PHONE_LIKE.match(raw) else ""
		digits = re.sub(r"\D", "", e164) if e164 else ""
		if not digits:
			digits = re.sub(r"\D", "", raw)
		key = digits
	else:
		key = raw
	if len(key) > WAIT_KEY_MAX_LENGTH:
		return ""
	return key


def reader_wait_keys(doctype: str, fieldname: str, value) -> list[str]:
	if value is None:
		return []
	is_phone = _is_phone_field(doctype, fieldname)
	spellings = _match_values(value, is_phone)
	if is_phone:
		keys = [re.sub(r"\D", "", s) for s in spellings if s and re.sub(r"\D", "", s)]
	else:
		keys = [str(s).strip() for s in spellings if str(s).strip()]
	return list(dict.fromkeys(keys))


def event_met(step, doc, run_started_at=None) -> bool:
	if not doc:
		return False

	doctype = getattr(doc, "doctype", None) or (doc.get("doctype") if isinstance(doc, dict) else None)
	docname = getattr(doc, "name", None) or (doc.get("name") if isinstance(doc, dict) else None)

	# Always re-read the triggering document fresh from the database if it exists
	if doctype and docname and hasattr(frappe, "db") and frappe.db and frappe.db.exists(doctype, docname):
		trigger_doc = frappe.get_doc(doctype, docname)
	else:
		trigger_doc = doc

	raw_rules = g(step, "condition_rules")
	if isinstance(raw_rules, str):
		try:
			rules = json.loads(raw_rules) if raw_rules else []
		except Exception:
			rules = []
	else:
		rules = raw_rules or []

	logic = g(step, "condition_logic") or "All"
	check_doctype = g(step, "check_doctype")

	# Same-doc mode: rules evaluate on the triggering document
	if not check_doctype:
		return rules_pass(rules, logic, trigger_doc)

	# Linked-record mode: look for records of check_doctype matching check_link_field
	link_field = g(step, "check_link_field")
	if not link_field:
		return False

	match_field = g(step, "check_match_field") or "name"
	match_value = field_value(trigger_doc, match_field)
	values = _match_values(match_value, _is_phone_field(check_doctype, link_field)) if match_value else []
	if not values:
		return False

	filters = {link_field: ("in", values)}
	if run_started_at:
		filters["creation"] = (">=", run_started_at)
	if check_doctype == doctype and docname:
		filters["name"] = ("!=", docname)

	valid_rules = [r for r in rules if isinstance(r, dict) and r.get("field")]
	if not valid_rules:
		return bool(frappe.db.exists(check_doctype, filters))

	for candidate_name in frappe.get_all(check_doctype, filters=filters, pluck="name", limit=EVENT_CANDIDATE_LIMIT):
		candidate_doc = frappe.get_doc(check_doctype, candidate_name)
		if rules_pass(valid_rules, logic, candidate_doc):
			return True

	return False
