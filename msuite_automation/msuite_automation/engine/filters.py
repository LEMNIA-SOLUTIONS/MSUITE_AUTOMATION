"""Filter / If-Else rule evaluation shared by the engine and the API."""

from __future__ import annotations

import frappe
from frappe.model.document import Document

ALIASES = {"awb_number": "lr_no", "lr_no": "awb_number"}


def normalize(v) -> str:
	return "" if v is None else str(v).strip()


def _get(obj, field, default=None):
	if obj is None:
		return default
	val = obj.get(field) if hasattr(obj, "get") else getattr(obj, field, default)
	return default if val is None else val


def field_value(doc, field: str):
	"""Value of `field` on doc; `sales_order.<f>` reads the Delivery Note's linked SO; AWB/LR alias."""
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
