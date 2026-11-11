"""Unified Automations API for the builder (Phase 2 frontend)."""

from __future__ import annotations

import json

import frappe
from frappe import _

from msuite_core.constants import (
	IG_CONSENT_IMAGE_SOURCE_POST,
	MESSENGER_BUTTON_TITLE_MAX,
	MESSENGER_TEMPLATE_SUBTITLE_MAX,
	MESSENGER_TEMPLATE_TITLE_MAX,
)
from msuite_automation.msuite_automation.constants import (
	IG_CONSENT_DEFAULT_EXPIRY_DAYS,
	IG_CONSENT_MIN_EXPIRY_DAYS,
)
from msuite_inbox.inbox.services.channels import messenger_templates
from msuite_automation.msuite_automation.constants import (
	DATE_BASED, DOCTYPE, DOCUMENT_EVENT, FIELD_CHANGE, RUN_DOCTYPE, RUN_ERROR_MAX, RUN_LIST_DEFAULT_LIMIT, RUN_LIST_MAX_LIMIT, SCHEDULE,
	SEND_EMAIL, SEND_SMS, SEND_WHATSAPP,
)
from msuite_automation.msuite_automation.engine import preview
from msuite_core.services.permissions.checks import check_delete, check_read, check_write

CHILD_TABLES = ("triggers", "filters", "steps", "variable_mappings", "edges", "recipients", "notes")
CHANNEL_LABEL = {SEND_WHATSAPP: "WA", SEND_EMAIL: "Email", SEND_SMS: "SMS"}
NO_VALUE_TYPES = frappe.model.no_value_fields + frappe.model.table_fields
STANDARD_DOC_FIELDS = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "doctype"}
SKIP_FIELDS = STANDARD_DOC_FIELDS | {"idx", *CHILD_TABLES}
CHILD_SKIP_FIELDS = STANDARD_DOC_FIELDS | {"parent", "parenttype", "parentfield"}


def _trigger_text(t) -> str:
	g = t.get
	kind, dt = g("trigger_type"), g("reference_doctype") or ""
	if kind == FIELD_CHANGE:
		if not g("field"):
			return dt
		text = f"{(g('field') or '').replace('_', ' ').title()} → {g('to_value') or 'any'}"
	elif kind == DOCUMENT_EVENT:
		text = g("doc_event") or ""
	elif kind == DATE_BASED:
		text = f"{g('offset_days') or 0} days {(g('offset_direction') or 'Before').lower()} {g('date_field') or ''}".strip() if g("date_field") else ""
	elif kind == SCHEDULE:
		return f"Every {(g('schedule_frequency') or '').lower()}" + (f" · {dt}" if dt else "")
	else:
		text = ""
	return f"{dt}: {text}" if dt and text else (text or dt)


def when_summary(doc) -> str:
	triggers = doc.get("triggers") or []
	if not triggers:
		return ""
	first = _trigger_text(triggers[0])
	return f"{first} + {len(triggers) - 1} more" if len(triggers) > 1 else first


def _dispatched() -> int:
	return frappe.utils.cint(frappe.db.sql(f"select coalesce(sum(sent_count), 0) from `tab{RUN_DOCTYPE}`")[0][0])


def _list_filters(search="", trigger_type="", enabled="", reference_doctype=""):
	filters = {}
	trig = {k: v for k, v in (("trigger_type", trigger_type), ("reference_doctype", reference_doctype)) if v}
	if trig:
		names = frappe.get_all("Automation Trigger", filters={"parenttype": DOCTYPE, **trig}, pluck="parent", distinct=True)
		filters["name"] = ("in", names or [""])
	if enabled != "" and enabled is not None:
		filters["disabled"] = 0 if frappe.utils.cint(enabled) else 1
	like = f"%{search.strip()}%" if search and search.strip() else None
	or_filters = {"title": ("like", like), "name": ("like", like)} if like else None
	return filters, or_filters


@frappe.whitelist()
def get_automations(search: str = "", trigger_type: str = "", enabled: str = "", reference_doctype: str = "", start: int = 0, page_length: int = 0) -> dict:
	check_read(DOCTYPE)
	filters, or_filters = _list_filters(search, trigger_type, enabled, reference_doctype)
	start, page_length = max(frappe.utils.cint(start), 0), max(frappe.utils.cint(page_length), 0)
	# ponytail: counts by plucking names; switch to a COUNT query if a site reaches ~10k automations
	total_count = len(frappe.get_all(DOCTYPE, filters=filters, or_filters=or_filters, pluck="name"))
	rows = frappe.get_all(DOCTYPE, filters=filters, or_filters=or_filters, order_by="modified desc, name desc",
		limit_start=start, limit_page_length=page_length,
		fields=["name", "title", "disabled", "modified"])
	if not rows:
		return {"items": [], "total_count": total_count}
	names = [r.name for r in rows]
	trig = {}
	for t in frappe.get_all("Automation Trigger", filters={"parent": ("in", names), "parenttype": DOCTYPE},
			fields=["parent", "trigger_type", "reference_doctype", "doc_event", "field", "to_value", "date_field", "offset_days", "offset_direction", "schedule_frequency"], order_by="idx asc"):
		trig.setdefault(t.parent, []).append(t)
	chans = {}
	for s in frappe.get_all("Automation Step", filters={"parent": ("in", names), "parenttype": DOCTYPE,
			"step_type": ("in", list(CHANNEL_LABEL))}, fields=["parent", "step_type"]):
		chans.setdefault(s.parent, set()).add(CHANNEL_LABEL[s.step_type])
	last = {}
	for r in frappe.db.sql(f"""select automation, status, creation, log from `tab{RUN_DOCTYPE}` r
			where creation = (select max(creation) from `tab{RUN_DOCTYPE}` where automation = r.automation)
			and automation in %(names)s""", {"names": names}, as_dict=True):
		entry = {"status": r.status, "at": str(r.creation)}
		if r.status == "Failed":
			try:
				parsed_log = json.loads(r.log or "[]")
			except Exception:
				parsed_log = []
			entry["error"] = _last_run_error(parsed_log)
		last[r.automation] = entry
	items = []
	for r in rows:
		triggers = trig.get(r.name, [])
		first = triggers[0] if triggers else None
		items.append({
			"name": r.name,
			"title": r.title,
			"disabled": r.disabled,
			"trigger_type": first.trigger_type if first else None,
			"reference_doctype": first.reference_doctype if first else None,
			"trigger_types": sorted({t.trigger_type for t in triggers if t.trigger_type}),
			"doctypes": sorted({t.reference_doctype for t in triggers if t.reference_doctype}),
			"when_summary": when_summary({"triggers": triggers}),
			"channels": [c for c in ("WA", "Email", "SMS") if c in chans.get(r.name, ())],
			"last_run": last.get(r.name),
			"modified": str(r.modified),
		})
	return {"items": items, "total_count": total_count}


@frappe.whitelist()
def get_automation(name: str) -> dict:
	check_read(DOCTYPE)
	doc = frappe.get_doc(DOCTYPE, name)
	doc.apply_fieldlevel_read_permissions()
	return doc.as_dict()


def _keep_saved_conditions(data: dict) -> bool:
	"""condition is permlevel 1: without write access, only the saved script may run or be kept.
	Returns True when the incoming conditions were replaced."""
	if 1 in frappe.get_meta(DOCTYPE).get_permlevel_access("write"):
		return False
	name = data.get("name")
	saved = {r.trigger_id: r.condition for r in frappe.get_all("Automation Trigger",
		filters={"parent": name, "parenttype": DOCTYPE}, fields=["trigger_id", "condition"])} if name else {}
	for t in data.get("triggers") or []:
		t["condition"] = saved.get(t.get("trigger_id")) or ""
	return True


@frappe.whitelist(methods=["POST"])
def save_automation(doc) -> dict:
	check_write(DOCTYPE)
	data = json.loads(doc) if isinstance(doc, str) else dict(doc)
	locked = _keep_saved_conditions(data)
	name = data.get("name")
	if name and frappe.db.exists(DOCTYPE, name):
		target = frappe.get_doc(DOCTYPE, name)
		target.check_permission("write")
	else:
		target = frappe.new_doc(DOCTYPE)
	target.update({k: v for k, v in data.items() if k not in SKIP_FIELDS})
	for table in CHILD_TABLES:
		clean = [{k: v for k, v in row.items() if k not in CHILD_SKIP_FIELDS} for row in data.get(table) or []]
		target.set(table, clean)
	if locked:
		for r in target.triggers:
			r.flags.ignore_permlevel_for_fields = ["condition"]
	target.save() if not target.is_new() else target.insert()
	return {"name": target.name}


@frappe.whitelist(methods=["POST"])
def toggle_automation(name: str, enabled) -> dict:
	check_write(DOCTYPE)
	doc = frappe.get_doc(DOCTYPE, name)
	doc.disabled = 0 if frappe.utils.cint(enabled) else 1
	doc.save()
	return {"name": name, "disabled": doc.disabled}


@frappe.whitelist(methods=["POST"])
def duplicate_automation(name: str) -> dict:
	check_write(DOCTYPE)
	src = frappe.get_doc(DOCTYPE, name)
	copy = frappe.copy_doc(src)
	copy.title = f"{src.title} (copy)"
	copy.disabled = 1
	copy.flags.ignore_permlevel_for_fields = ["condition"]
	for t in copy.triggers:
		t.flags.ignore_permlevel_for_fields = ["condition"]
	copy.insert()
	return {"name": copy.name}


@frappe.whitelist(methods=["POST"])
def delete_automation(name: str) -> dict:
	check_delete(DOCTYPE)
	if not frappe.db.exists(DOCTYPE, name):
		raise frappe.DoesNotExistError
	frappe.delete_doc(DOCTYPE, name)
	return {"name": name}


@frappe.whitelist(methods=["POST"])
def bulk_delete_automations(names) -> dict:
	check_delete(DOCTYPE)
	names = json.loads(names) if isinstance(names, str) else names
	deleted, failed = [], []
	for n in names or []:
		if not frappe.db.exists(DOCTYPE, n):
			failed.append({"name": n, "error": "Not found"})
			continue
		try:
			frappe.delete_doc(DOCTYPE, n)
			deleted.append(n)
		except Exception as e:
			failed.append({"name": n, "error": str(e)})
	return {"deleted": deleted, "failed": failed}


def _fields(doctype: str, prefix: str = "", label_prefix: str = "") -> list[dict]:
	"""Retrieve fields of a DocType formatted for automation field pickers.

	Args:
		doctype: Name of the DocType to read fields from.
		prefix: Fieldname prefix to prepend (e.g. "sales_order.").
		label_prefix: Human-readable prefix for labels (e.g. "Sales Order › ").

	Returns:
		list[dict]: Field dictionaries with fieldname, label, fieldtype, options, and reqd.

	Raises:
		None.

	Side Effects:
		None.
	"""
	out = [{"fieldname": f"{prefix}name", "label": f"{label_prefix}ID", "fieldtype": "Data", "options": None, "reqd": 1}]
	for df in frappe.get_meta(doctype).fields:
		if df.fieldtype in NO_VALUE_TYPES or df.hidden:
			continue
		out.append({"fieldname": prefix + df.fieldname, "label": label_prefix + _(df.label or df.fieldname),
			"fieldtype": df.fieldtype, "options": df.options, "reqd": int(bool(df.reqd))})
	return out


@frappe.whitelist()
def get_doctype_fields(doctype: str) -> list[dict]:
	check_read(DOCTYPE)
	if not frappe.has_permission(doctype, "read"):
		raise frappe.PermissionError
	out = _fields(doctype)
	if doctype == "Delivery Note":
		out += _fields("Sales Order", "sales_order.", "Sales Order › ")
	return out


@frappe.whitelist()
def get_ig_step_meta() -> dict:
	"""Limits and special values the builder needs for the Instagram mention steps.

	Returns:
		dict: title_max, subtitle_max, button_title_max, expiry_min, expiry_default,
			image_source_post, templates_enabled.

	Raises:
		frappe.PermissionError: Caller cannot read Messaging Automation.

	Side Effects:
		None.
	"""
	check_read(DOCTYPE)
	return {
		"title_max": MESSENGER_TEMPLATE_TITLE_MAX,
		"subtitle_max": MESSENGER_TEMPLATE_SUBTITLE_MAX,
		"button_title_max": MESSENGER_BUTTON_TITLE_MAX,
		"expiry_min": IG_CONSENT_MIN_EXPIRY_DAYS,
		"expiry_default": IG_CONSENT_DEFAULT_EXPIRY_DAYS,
		"image_source_post": IG_CONSENT_IMAGE_SOURCE_POST,
		"templates_enabled": messenger_templates.is_templates_enabled(),
	}


@frappe.whitelist()
def get_condition_edit_access(name: str | None = None) -> bool:
	"""Check whether the current user has permission to edit automation conditions.

	Enforces read permission, role write access, document ownership (when name is
	provided), and permlevel 1 write access where condition fields reside.

	Args:
		name: Optional document name of a Messaging Automation record.

	Returns:
		bool: True if caller has write access and permlevel 1 access to edit conditions;
			False otherwise.

	Raises:
		frappe.PermissionError: Caller cannot read Messaging Automation or is a Guest.

	Side Effects:
		None.
	"""
	check_read(DOCTYPE, name)
	if name:
		if not frappe.has_permission(DOCTYPE, "write", doc=name):
			return False
	else:
		if not frappe.has_permission(DOCTYPE, "write"):
			return False
	return 1 in frappe.get_meta(DOCTYPE).get_permlevel_access("write")



@frappe.whitelist()
def get_reference_doctypes() -> list[str]:
	check_read(DOCTYPE)
	readable = set(frappe.get_user().get_can_read())
	return [
		d for d in frappe.get_all("DocType", filters={"istable": 0, "issingle": 0}, pluck="name", order_by="name asc")
		if d in readable
	]


@frappe.whitelist()
def test_automation(doc, docname: str, trigger_id: str = "") -> dict:
	"""Builder Test button: dry run of the current (possibly unsaved) automation
	against one record. Sends nothing, writes nothing."""
	check_read(DOCTYPE)
	data = frappe.parse_json(doc) if isinstance(doc, str) else dict(doc)
	_keep_saved_conditions(data)
	automation = frappe.get_doc({**data, "doctype": DOCTYPE})
	trig = next((t for t in automation.triggers if t.trigger_id == trigger_id), None) or (automation.triggers[0] if automation.triggers else None)
	dt = trig.reference_doctype if trig else None
	if not dt:
		frappe.throw(_("Choose a DocType first"))
	if not frappe.db.exists(dt, docname):
		frappe.throw(_("{0} {1} not found").format(_(dt), docname), frappe.DoesNotExistError)
	target = frappe.get_doc(dt, docname)
	target.check_permission("read")
	return preview.dry_run(automation, target, trig.trigger_id)


def _last_run_error(log_entries: list) -> str:
	"""Extract the most recent step error message from a run log.

	Args:
		log_entries: List of log entry dictionaries from the run.

	Returns:
		str: Error message truncated to RUN_ERROR_MAX, or empty string.

	Raises:
		None.

	Side Effects:
		None.
	"""
	for entry in reversed(log_entries or []):
		if isinstance(entry, dict) and entry.get("error"):
			return str(entry["error"])[:RUN_ERROR_MAX]
	return ""


@frappe.whitelist()
def get_runs(name: str, limit: int = RUN_LIST_DEFAULT_LIMIT) -> list[dict]:
	"""Retrieve execution runs for an automation with Channel Message delivery status merged.

	Args:
		name: Name of the Messaging Automation document.
		limit: Maximum number of run records to retrieve.

	Returns:
		list[dict]: List of run dictionaries with parsed logs and merged delivery statuses.

	Raises:
		frappe.PermissionError: If user lacks read permission on Messaging Automation.

	Side Effects:
		None. Live join on read; no database write-back.
	"""
	check_read(DOCTYPE)
	if not frappe.has_permission(DOCTYPE, "read", doc=name):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	rows = frappe.get_all(RUN_DOCTYPE, filters={"automation": name}, order_by="creation desc",
		limit=max(1, min(frappe.utils.cint(limit) or RUN_LIST_DEFAULT_LIMIT, RUN_LIST_MAX_LIMIT)),
		fields=["name", "status", "trigger_id", "reference_doctype", "reference_name", "creation", "resume_at", "sent_count", "log"])
	msg_names = set()
	for r in rows:
		try:
			r.log = json.loads(r.log or "[]")
		except ValueError:
			r.log = []
		r.creation = str(r.creation)
		r.resume_at = str(r.resume_at) if r.resume_at else None
		r["error"] = _last_run_error(r.log) if r.status == "Failed" else ""
		for entry in r.log:
			if isinstance(entry, dict) and entry.get("message"):
				msg_names.add(entry["message"])

	if msg_names:
		cm_rows = frappe.get_all(
			"Channel Message",
			filters={"name": ["in", list(msg_names)]},
			fields=["name", "status", "status_error"],
		)
		cm_by_name = {cm["name"]: cm for cm in cm_rows}
		for r in rows:
			for entry in r.log:
				if isinstance(entry, dict) and entry.get("message") in cm_by_name:
					cm = cm_by_name[entry["message"]]
					if cm.get("status"):
						entry["status"] = cm["status"]
					if cm.get("status_error"):
						entry["status_error"] = cm["status_error"]

	return rows


@frappe.whitelist(methods=["POST"])
def retry_run(run_name: str) -> dict:
	"""Retry a failed automation execution run by queuing a new run.

	Note:
		A run where only some steps failed can resend already-sent steps (existing
		already_sent/retrying guards limit this). A retry does not count toward the
		monthly run plan gate.

	Args:
		run_name: Name of the failed Messaging Automation Run document.

	Returns:
		dict: {"name": str} with the newly queued run identifier.

	Raises:
		frappe.PermissionError: If user lacks write permission on Messaging Automation.
		frappe.DoesNotExistError: If run_name does not exist.
		frappe.ValidationError: If run is not Failed or context is empty/invalid.

	Side Effects:
		Inserts an Automation Outbox record and schedules delivery.
	"""
	check_write(DOCTYPE)
	if not run_name or not frappe.db.exists(RUN_DOCTYPE, run_name):
		frappe.throw(_("Automation run {0} not found").format(run_name), frappe.DoesNotExistError)
	run = frappe.get_doc(RUN_DOCTYPE, run_name)
	if not frappe.has_permission(DOCTYPE, "write", doc=run.automation):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if run.status != "Failed":
		frappe.throw(_("Only failed runs can be retried"))
	from msuite_automation.msuite_automation.engine.executor import _json, enqueue_run
	ctx = _json(run.context, None)
	if not ctx or not isinstance(ctx, dict):
		frappe.throw(_("Cannot retry run with empty or invalid context"))
	new_name = enqueue_run(
		name=run.automation,
		trigger_id=run.trigger_id,
		doctype=run.reference_doctype,
		docname=run.reference_name,
		value=ctx.get("value", ""),
		discriminator=ctx.get("discriminator", ""),
		doc_dict=ctx.get("doc_dict"),
		skip_checks=True,
		phone=ctx.get("phone"),
		retry=True,
	)
	return {"name": new_name}


@frappe.whitelist()
def get_stats() -> dict:
	"""Global list-page numbers (not affected by the list's filters or paging)."""
	check_read(DOCTYPE)
	rows = frappe.get_all(DOCTYPE, fields=["name", "disabled"])
	trig = frappe.get_all("Automation Trigger", filters={"parenttype": DOCTYPE}, fields=["parent", "trigger_type", "reference_doctype"])
	by_trigger: dict = {}
	for kind in {(t.parent, t.trigger_type) for t in trig if t.trigger_type}:
		by_trigger[kind[1]] = by_trigger.get(kind[1], 0) + 1
	return {"total": len(rows), "active": sum(1 for r in rows if not r.disabled), "dispatched": _dispatched(),
		"by_trigger": by_trigger, "doctypes": sorted({t.reference_doctype for t in trig if t.reference_doctype})}
