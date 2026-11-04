"""Trigger entry points: doc events ("*" hook), the order-app order-status
signal, and scheduler ticks. Everything here only decides *which*
automation runs for *which* doc; executor.py runs it."""

from __future__ import annotations

import frappe
from frappe.core.doctype.server_script.server_script_utils import EVENT_MAP
from frappe.utils import add_to_date, nowdate

TRIGGER_EVENT_MAP = {**EVENT_MAP, "on_trash": "On Trash"}

from msuite_automation.msuite_automation.constants import (
	DATE_BASED, DOCTYPE, DOCUMENT_EVENT, FIELD_CHANGE, QUEUE_SHORT, SCHEDULE,
)
from msuite_automation.msuite_automation.engine.filters import _get, normalize

CACHE_KEY = "messaging_automation_trigger_map"
WATCH_CACHE_KEY = "messaging_automation_watch_map"
SKIP_DOCTYPES = {"DocType", "DocField", "DocPerm", "Custom Field", "Property Setter", "Module Def", DOCTYPE,
	"Automation Run", "Messaging Automation Run", "Error Log", "Scheduled Job Log", "Version", "Comment", "Channel Message"}
# Channel Message never fires trigger automations (our own sends create them), but a Wait-for-Event may be waiting on a reply.
WATCH_SKIP_DOCTYPES = SKIP_DOCTYPES - {"Channel Message"}
FIELD_EVENTS = {"after_insert", "on_update", "on_update_after_submit", "on_cancel"}
WATCH_METHODS = {"after_insert", "on_update", "on_update_after_submit"}
ALIASES = {"lr_no": "awb_number", "awb_number": "lr_no"}
ANY_CHANGE = "Assigned / Changed"  # legacy target value that may still arrive via old caches
CHECK_EVENT_WAITS_PATH = "msuite_automation.msuite_automation.engine.executor.check_event_waits"


def clear_cache() -> None:
	frappe.cache().delete_value(CACHE_KEY)
	frappe.cache().delete_value(WATCH_CACHE_KEY)


def get_watch_map() -> dict:
	cached = frappe.cache().get_value(WATCH_CACHE_KEY)
	if cached is not None:
		return cached
	out: dict = {}
	from msuite_automation.msuite_automation.constants import WAIT_EVENT

	step_rows = frappe.db.sql("""
		select s.parent as automation, s.step_id, s.check_doctype, s.check_link_field
		from `tabAutomation Step` s
		join `tabMessaging Automation` a on a.name = s.parent
		where s.parenttype=%s and a.disabled=0 and s.step_type=%s
		order by s.idx
	""", (DOCTYPE, WAIT_EVENT), as_dict=True)

	if step_rows:
		trigger_rows = frappe.db.sql("""
			select t.parent as automation, t.reference_doctype
			from `tabAutomation Trigger` t
			join `tabMessaging Automation` a on a.name = t.parent
			where t.parenttype=%s and a.disabled=0 and ifnull(t.reference_doctype, '') != ''
			order by t.idx
		""", (DOCTYPE,), as_dict=True)
		triggers_by_auto: dict[str, set[str]] = {}
		for tr in trigger_rows:
			triggers_by_auto.setdefault(tr.automation, set()).add(tr.reference_doctype)

		for s in step_rows:
			check_dt = (s.check_doctype or "").strip()
			if check_dt:
				entry = (s.automation, s.step_id, "linked", (s.check_link_field or "").strip())
				out.setdefault(check_dt, []).append(entry)
			else:
				for ref_dt in triggers_by_auto.get(s.automation, ()):
					if ref_dt:
						entry = (s.automation, s.step_id, "same_doc", "")
						out.setdefault(ref_dt, []).append(entry)

		for dt in out:
			out[dt] = list(dict.fromkeys(out[dt]))

	frappe.cache().set_value(WATCH_CACHE_KEY, out)
	return out


def get_map() -> dict:
	cached = frappe.cache().get_value(CACHE_KEY)
	if cached is not None:
		return cached
	out: dict = {}
	for r in frappe.db.sql("""select t.parent, t.trigger_id, t.trigger_type, t.reference_doctype, t.doc_event,
			t.field, t.from_value, t.to_value
			from `tabAutomation Trigger` t join `tabMessaging Automation` a on a.name = t.parent
			where t.parenttype=%s and a.disabled=0 and t.trigger_type in (%s, %s) and ifnull(t.reference_doctype, '') != ''
			order by t.idx""", (DOCTYPE, DOCUMENT_EVENT, FIELD_CHANGE), as_dict=True):
		entry = out.setdefault(r.reference_doctype, {"events": {}, "fields": []})
		if r.trigger_type == DOCUMENT_EVENT and r.doc_event:
			entry["events"].setdefault(r.doc_event, []).append([r.parent, r.trigger_id])
		elif r.trigger_type == FIELD_CHANGE and r.field:
			entry["fields"].append([r.parent, r.trigger_id, r.field, r.from_value or "", r.to_value or ""])
	frappe.cache().set_value(CACHE_KEY, out)
	return out


def build_diff(doc, fields: set[str], before) -> dict:
	"""{field: {"old", "new", [delivery_attempts]}} for watched fields that changed.
	before=None means a new document (old = "")."""
	diff = {}
	watch = set(fields) | {ALIASES[f] for f in fields if f in ALIASES}
	for f in watch:
		old = "" if before is None else _get(before, f)
		new = _get(doc, f)
		if normalize(old) != normalize(new) and (before is not None or normalize(new)):
			diff[f] = {"old": old, "new": new}
	if before is not None and "carrier_tracking_status" in watch and _get(doc, "carrier_tracking_status") == "FAILED_ATTEMPT":
		old_att, new_att = _get(before, "delivery_attempts"), _get(doc, "delivery_attempts")
		if "carrier_tracking_status" in diff:
			diff["carrier_tracking_status"]["delivery_attempts"] = new_att
		elif old_att != new_att:
			diff["carrier_tracking_status"] = {"old": f"FAILED_ATTEMPT_{old_att}", "new": "FAILED_ATTEMPT", "delivery_attempts": new_att}
	if not diff and _get(doc, "docstatus") == 2 and "status" in watch:
		diff["status"] = {"old": "", "new": "Cancelled"}
	return diff


def match(field_rows, diff: dict) -> list[tuple]:
	hits = []
	for name, tid, field, from_value, to_value in field_rows:
		change = diff.get(field) or diff.get(ALIASES.get(field, ""))
		if not change:
			continue
		old, new = change.get("old"), change.get("new")
		if normalize(old) == normalize(new):
			continue
		if from_value and normalize(old).lower() != normalize(from_value).lower() and not (from_value == "FAILED_ATTEMPT" and str(old).startswith("FAILED_ATTEMPT")):
			continue
		if to_value and to_value != ANY_CHANGE:
			if normalize(new).lower() != normalize(to_value).lower():
				continue
		elif not normalize(new):
			continue
		disc = str(change.get("delivery_attempts") or "") if str(new) == "FAILED_ATTEMPT" else ""
		hits.append((name, tid, new, disc))
	return hits


def _enqueue_field_hits(doctype, docname, entry, diff):
	from msuite_automation.msuite_automation.engine.executor import enqueue_run

	for name, tid, new, disc in match(entry["fields"], diff):
		enqueue_run(name=name, trigger_id=tid, doctype=doctype, docname=docname, value=str(new), discriminator=disc)


def on_doc_event(doc, method: str) -> None:
	"""The "*" doc_events hook. Cheap exit when the doctype has no automations."""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_uninstall:
		return
	doctype = getattr(doc, "doctype", None)
	if not doctype or doctype in WATCH_SKIP_DOCTYPES:
		return
	try:
		flags = getattr(doc, "flags", None) or {}
		if method in WATCH_METHODS and not (method == "on_update" and _get(flags, "in_insert")) and doctype in get_watch_map():
			docname = getattr(doc, "name", None)
			if docname:
				from msuite_automation.msuite_automation.engine.executor import enqueue_safely

				enqueue_safely(CHECK_EVENT_WAITS_PATH,
					queue=QUEUE_SHORT,
					job_id=f"check_event_waits:{doctype}:{docname}", deduplicate=True,
					doctype=doctype, name=docname)

		if doctype in SKIP_DOCTYPES:
			return
		entry = get_map().get(doctype)
		if not entry:
			return
		if _get(flags, "ignore_order_automation"):
			return
		from msuite_automation.msuite_automation.engine.executor import enqueue_run

		ev_name = TRIGGER_EVENT_MAP.get(method)
		hits = list(entry["events"].get(ev_name, ()))
		if method == "on_trash":
			hits += [h for h in entry["events"].get("Before Delete", ()) if h not in hits]
		for name, tid in hits:
			if method in ("on_trash", "after_delete", "before_insert"):
				enqueue_run(name=name, trigger_id=tid, doctype=doctype, docname=doc.name, doc_dict=doc.as_dict())
			else:
				enqueue_run(name=name, trigger_id=tid, doctype=doctype, docname=doc.name)

		if entry["fields"] and method in FIELD_EVENTS:
			if method == "on_update" and _get(flags, "in_insert"):
				return  # after_insert already evaluated this save
			fields = {r[2] for r in entry["fields"]}
			# cancel/update-after-submit go through save(), so the snapshot exists there too
			before = None if method == "after_insert" else (doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None)
			if before is None and method != "after_insert":
				return  # no snapshot (db_set path) — the order-app signal covers those
			diff = build_diff(doc, fields, before)
			if diff:
				_enqueue_field_hits(doctype, doc.name, entry, diff)
	except Exception:
		frappe.log_error(title=f"Messaging Automation trigger failed: {doctype} {getattr(doc, 'name', '')}")


def on_order_status_changed(delivery_note=None, dn_diff=None, so_diffs=None, **kwargs) -> None:
	"""Order-app `order_status_changed_listeners` — status changes written with db_set."""
	try:
		m = get_map()
		if delivery_note and dn_diff and m.get("Delivery Note"):
			_enqueue_field_hits("Delivery Note", delivery_note, m["Delivery Note"], dn_diff)
		for so_name, diff in (so_diffs or {}).items():
			if diff and m.get("Sales Order"):
				_enqueue_field_hits("Sales Order", so_name, m["Sales Order"], diff)
	except Exception:
		frappe.log_error(title="on_order_status_changed failed")


def _trigger_rows(trigger_type, **filters):
	return frappe.db.sql("""select t.parent, t.trigger_id, t.reference_doctype, t.date_field, t.offset_days, t.offset_direction, a.send_frequency
		from `tabAutomation Trigger` t join `tabMessaging Automation` a on a.name = t.parent
		where t.parenttype=%(dt)s and a.disabled=0 and t.trigger_type=%(tt)s""" + "".join(f" and t.{k}=%({k})s" for k in filters),
		{"dt": DOCTYPE, "tt": trigger_type, **filters}, as_dict=True)


def run_schedule(frequency: str) -> None:
	from msuite_automation.msuite_automation.engine.executor import run_scheduled

	for t in _trigger_rows(SCHEDULE, schedule_frequency=frequency):
		try:
			run_scheduled(t.parent, t.trigger_id)
		except Exception:
			frappe.log_error(title=f"Messaging Automation scheduler failed: {t.parent}")


def run_date_based() -> None:
	from msuite_automation.msuite_automation.engine.executor import enqueue_pending

	for t in _trigger_rows(DATE_BASED):
		try:
			if not (t.reference_doctype and t.date_field):
				continue
			days = int(t.offset_days or 0) * (1 if t.offset_direction == "Before" else -1)
			day = add_to_date(nowdate(), days=days)
			base_filters = [[t.date_field, ">=", f"{day} 00:00:00"], [t.date_field, "<=", f"{day} 23:59:59.999999"]]
			enqueue_pending(t.parent, t.trigger_id, t.reference_doctype, t.send_frequency, base_filters)
		except Exception:
			frappe.log_error(title=f"Messaging Automation date trigger failed: {t.parent}")


def _tick(freq):
	return lambda: run_schedule(freq)


schedule_all = _tick("All")
schedule_hourly = _tick("Hourly")
schedule_daily = _tick("Daily")
schedule_weekly = _tick("Weekly")
schedule_monthly = _tick("Monthly")
schedule_yearly = _tick("Yearly")
schedule_hourly_long = _tick("Hourly Long")
schedule_daily_long = _tick("Daily Long")
schedule_weekly_long = _tick("Weekly Long")
schedule_monthly_long = _tick("Monthly Long")
