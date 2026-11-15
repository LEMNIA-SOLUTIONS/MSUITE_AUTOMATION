"""Filter / If-Else rule evaluation shared by the engine and the API."""

from __future__ import annotations

import frappe
from frappe.model.document import Document

from msuite_automation.msuite_automation.constants import (
	CONTACT_DOCTYPE,
	DEAL_CONTACTS_CHILD_DOCTYPE,
	DEAL_DOCTYPE,
	DEAL_PRIMARY_CONTACT_MOBILE,
	DEAL_PRIMARY_CONTACT_MOBILE_ALT,
	FIELD_MOBILE_NO,
)

ALIASES = {"awb_number": "lr_no", "lr_no": "awb_number"}


def normalize(v) -> str:
	return "" if v is None else str(v).strip()


def _get(obj, field, default=None):
	if obj is None:
		return default
	val = obj.get(field) if hasattr(obj, "get") else getattr(obj, field, default)
	return default if val is None else val


def get_deal_primary_contact_mobile(doc) -> str | None:
	"""Fetch the mobile number of the primary contact linked to a CRM Deal.

	Checks the 'CRM Contacts' child rows for an is_primary row:
	first its own mobile_no, then the linked Contact's mobile_no.
	Also falls back to the Deal's direct contact link if present.

	Args:
		doc: CRM Deal document or dictionary.

	Returns:
		str | None: Cleaned mobile number string, or None if unresolvable.

	Raises:
		None.

	Side Effects:
		May query database for child rows or Contact document when not loaded in memory.
	"""
	if not doc:
		return None
	contacts = getattr(doc, "contacts", None)
	if contacts is None and isinstance(doc, dict):
		contacts = doc.get("contacts")

	primary_row = None
	if contacts:
		for row in contacts:
			is_pri = getattr(row, "is_primary", None) if hasattr(row, "is_primary") else (row.get("is_primary") if isinstance(row, dict) else None)
			if is_pri:
				primary_row = row
				break
	doc_name = getattr(doc, "name", None) or (doc.get("name") if isinstance(doc, dict) else None)
	if not primary_row and doc_name and getattr(frappe, "db", None):
		rows = frappe.get_all(
			DEAL_CONTACTS_CHILD_DOCTYPE,
			filters={"parent": doc_name, "parenttype": DEAL_DOCTYPE, "is_primary": 1},
			fields=[FIELD_MOBILE_NO, "contact"],
			limit=1,
		)
		if rows:
			primary_row = rows[0]

	if primary_row:
		mob = getattr(primary_row, FIELD_MOBILE_NO, None) if hasattr(primary_row, FIELD_MOBILE_NO) else (primary_row.get(FIELD_MOBILE_NO) if isinstance(primary_row, dict) else None)
		if mob and str(mob).strip():
			return str(mob).strip()
		contact_id = getattr(primary_row, "contact", None) if hasattr(primary_row, "contact") else (primary_row.get("contact") if isinstance(primary_row, dict) else None)
		if contact_id and getattr(frappe, "db", None):
			contact_mob = frappe.db.get_value(CONTACT_DOCTYPE, contact_id, FIELD_MOBILE_NO)
			if contact_mob and str(contact_mob).strip():
				return str(contact_mob).strip()

	contact_id = getattr(doc, "contact", None) or (doc.get("contact") if isinstance(doc, dict) else None)
	if contact_id and getattr(frappe, "db", None):
		contact_mob = frappe.db.get_value(CONTACT_DOCTYPE, contact_id, FIELD_MOBILE_NO)
		if contact_mob and str(contact_mob).strip():
			return str(contact_mob).strip()

	return None


def field_value(doc, field: str):
	"""Value of `field` on doc; `sales_order.<f>` reads the Delivery Note's linked SO; AWB/LR alias; deal primary contact mobile.

	Args:
		doc: Document or dictionary to read from.
		field: Name of the field to read, or compound field accessor.

	Returns:
		Any: Field value or None if absent.

	Raises:
		None.

	Side Effects:
		May query linked documents if accessor requires traversal.
	"""
	if field in (DEAL_PRIMARY_CONTACT_MOBILE, DEAL_PRIMARY_CONTACT_MOBILE_ALT):
		return get_deal_primary_contact_mobile(doc)
	target = doc
	if field.startswith("sales_order."):
		field, target = field[len("sales_order."):], get_linked_sales_order(doc)
		if not target:
			return None
	value = _get(target, field)
	if value is None and field in ALIASES:
		value = _get(target, ALIASES[field])
	return value


def rules_pass(rules: list[dict], logic: str, doc) -> bool:
	rules = [r for r in rules or [] if isinstance(r, dict) and r.get("field")]
	if not rules:
		return True
	results = (rule_ok(field_value(doc, r["field"]), r.get("operator") or "=", r.get("value")) for r in rules)
	return any(results) if logic == "Any" else all(results)


def rule_ok(actual, operator: str, expected) -> bool:
	if operator == "is set":
		return bool(normalize(actual))
	if operator == "is not set":
		return not normalize(actual)

	if operator == "contains":
		exp_str = normalize(expected).lower()
		return bool(exp_str) and exp_str in normalize(actual).lower()

	if operator == "in":
		items = {x.strip().lower() for x in str(expected or "").split(",") if x.strip()}
		return normalize(actual).lower() in items

	if operator in ("=", "!="):
		try:
			matched = float(actual) == float(expected)
		except (ValueError, TypeError):
			matched = normalize(actual) == normalize(expected)
		return matched if operator == "=" else not matched

	if operator in (">", "<", ">=", "<="):
		if not normalize(actual) or not normalize(expected):
			return False
		try:
			av, ev = float(actual), float(expected)
		except (ValueError, TypeError):
			av, ev = normalize(actual), normalize(expected)
		if operator == ">": return av > ev
		if operator == "<": return av < ev
		if operator == ">=": return av >= ev
		if operator == "<=": return av <= ev

	return False


def get_linked_sales_order(doc) -> Document | dict | None:
	"""Find linked Sales Order for Delivery Note via Delivery Note Item.against_sales_order first row."""
	if not doc:
		return None
	cached = getattr(doc, "_linked_sales_order", None)
	if cached:
		return cached

	so_name = None
	for item in _get(doc, "items") or []:
		so_ref = _get(item, "against_sales_order")
		if so_ref:
			so_name = so_ref
			break

	if not so_name:
		docname = _get(doc, "name")
		if docname and hasattr(frappe, "db") and frappe.db and hasattr(frappe.db, "exists"):
			try:
				if frappe.db.exists("Delivery Note", docname):
					so_name = frappe.db.get_value(
						"Delivery Note Item",
						{"parent": docname, "against_sales_order": ["is", "set"]},
						"against_sales_order",
					)
			except Exception:
				pass

	if so_name and hasattr(frappe, "db") and frappe.db and hasattr(frappe.db, "exists"):
		try:
			if frappe.db.exists("Sales Order", so_name):
				so_doc = frappe.get_doc("Sales Order", so_name)
				try:
					setattr(doc, "_linked_sales_order", so_doc)
				except Exception:
					pass
				return so_doc
		except Exception:
			pass

	return None
