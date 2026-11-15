"""Filter / If-Else rule evaluation shared by the engine and the API."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from msuite_automation.msuite_automation.constants import (
	CONTACT_DOCTYPE,
	DEAL_CONTACTS_CHILD_DOCTYPE,
	DEAL_DOCTYPE,
	DEAL_PRIMARY_CONTACT_MOBILE,
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

	When child rows are loaded in memory, picks the row where is_primary is true
	with the lowest idx. When child rows are not loaded, queries the database
	ordered by idx asc. If no primary contact row exists, returns None.

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

	contacts = _get(doc, "contacts")
	primary = None
	if contacts is not None:
		primaries = [r for r in contacts if _get(r, "is_primary")]
		if not primaries:
			return None
		primary = min(primaries, key=lambda r: cint(_get(r, "idx", 0)))
	elif _get(doc, "name"):
		rows = frappe.get_all(
			DEAL_CONTACTS_CHILD_DOCTYPE,
			filters={"parent": _get(doc, "name"), "parenttype": DEAL_DOCTYPE},
			fields=[FIELD_MOBILE_NO, "contact"],
			order_by="is_primary desc, idx asc",
			limit=1,
		)
		primary = rows[0] if rows else None

	if not primary:
		return None

	mob = _get(primary, FIELD_MOBILE_NO)
	val = str(mob).strip() if mob and str(mob).strip() else None
	if not val and _get(primary, "contact"):
		cmob = frappe.db.get_value(CONTACT_DOCTYPE, _get(primary, "contact"), FIELD_MOBILE_NO)
		val = str(cmob).strip() if cmob and str(cmob).strip() else None
	return val


def _resolve_delivery_note_sales_order(doc, field: str):
	"""Resolve sales_order.<fieldname> for Delivery Note via linked Sales Order.

	Args:
		doc: Delivery Note document or dictionary.
		field: Compound field accessor (e.g. sales_order.customer_name).

	Returns:
		Any: Field value from linked Sales Order or None.

	Raises:
		None.

	Side Effects:
		May query database to fetch linked Sales Order.
	"""
	prefix = "sales_order."
	subfield = field[len(prefix):] if field.startswith(prefix) else field
	so = get_linked_sales_order(doc)
	if not so:
		return None
	val = _get(so, subfield)
	if val is None and subfield in ALIASES:
		val = _get(so, ALIASES[subfield])
	return val


# ── Module-level virtual-field resolver table (Task 5 / D8) ─────────────────
VIRTUAL_FIELD_RESOLVERS = {
	(DEAL_DOCTYPE, DEAL_PRIMARY_CONTACT_MOBILE): {
		"resolve": lambda doc, field: get_deal_primary_contact_mobile(doc),
		"is_phone": True,
		"target": (CONTACT_DOCTYPE, FIELD_MOBILE_NO),
		"field_meta": {
			"fieldname": DEAL_PRIMARY_CONTACT_MOBILE,
			"label": _("Primary Contact Mobile"),
			"fieldtype": "Data",
			"options": "Phone",
			"reqd": 0,
		},
	},
	("Delivery Note", "sales_order.*"): {
		"resolve": _resolve_delivery_note_sales_order,
		"prefix": "sales_order.",
		"target_doctype": "Sales Order",
		"label_prefix": "Sales Order › ",
	},
}


def get_virtual_field_resolver(doctype: str | None, field: str) -> dict | None:
	"""Look up a virtual field resolver entry for (doctype, field).

	Args:
		doctype: DocType name or None.
		field: Field name.

	Returns:
		dict | None: Resolver dictionary from VIRTUAL_FIELD_RESOLVERS or None.

	Raises:
		None.

	Side Effects:
		None.
	"""
	if not field:
		return None
	if doctype and (doctype, field) in VIRTUAL_FIELD_RESOLVERS:
		return VIRTUAL_FIELD_RESOLVERS[(doctype, field)]
	if doctype == "Delivery Note" and field.startswith("sales_order."):
		return VIRTUAL_FIELD_RESOLVERS.get(("Delivery Note", "sales_order.*"))
	if not doctype:
		if field == DEAL_PRIMARY_CONTACT_MOBILE:
			return VIRTUAL_FIELD_RESOLVERS.get((DEAL_DOCTYPE, DEAL_PRIMARY_CONTACT_MOBILE))
		if field.startswith("sales_order."):
			return VIRTUAL_FIELD_RESOLVERS.get(("Delivery Note", "sales_order.*"))
	return None


def resolve_virtual_phone(doc) -> str | None:
	"""Resolve recipient phone number from virtual fields registered in VIRTUAL_FIELD_RESOLVERS.

	Args:
		doc: Trigger document or payload dictionary.

	Returns:
		str | None: Raw phone string resolved from a registered virtual field, or None.

	Raises:
		None.

	Side Effects:
		May query database via registered field resolver.
	"""
	if not doc:
		return None
	doctype = getattr(doc, "doctype", None) or (doc.get("doctype") if hasattr(doc, "get") else None)
	if not doctype:
		return None
	for (dt, field), entry in VIRTUAL_FIELD_RESOLVERS.items():
		if dt == doctype and entry.get("is_phone"):
			val = field_value(doc, field)
			if val:
				return str(val).strip()
	return None


def field_value(doc, field: str):
	"""Value of `field` on doc, reading virtual fields from VIRTUAL_FIELD_RESOLVERS.

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
	if not doc or not field:
		return None
	doc_dt = getattr(doc, "doctype", None) or (doc.get("doctype") if hasattr(doc, "get") else None)
	resolver = get_virtual_field_resolver(doc_dt, field)
	if resolver and resolver.get("resolve"):
		return resolver["resolve"](doc, field)
	value = _get(doc, field)
	if value is None and field in ALIASES:
		value = _get(doc, ALIASES[field])
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
