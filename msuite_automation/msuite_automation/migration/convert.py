"""Pure converters: legacy Messaging Automation row → unified automation.

No frappe calls — the patch reads rows with raw SQL and hands dicts here.
`has_status` / `has_workflow_state` on lead rows are precomputed by the patch
from the reference DocType's meta.
"""

from __future__ import annotations

import json

AWB_FIELDS = ("lr_no", "awb_number", "unicommerce_manifest_id")
MAX_DEPTH = 3
OP_LOGIC = {"AND": "All", "OR": "Any"}


def _new(values=None):
	return {"values": values or {}, "triggers": [], "filters": [], "steps": [], "mappings": [], "problems": []}


def _mapping_rows(step_id, items):
	out = []
	if isinstance(items, str):
		try:
			items = json.loads(items)
		except ValueError:
			items = [f.strip() for f in items.split(",") if f.strip()]
	if isinstance(items, dict):
		items = [dict(v, variable_key=k) if isinstance(v, dict) else {"variable_key": k, "source_field": v}
			for k, v in sorted(items.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else str(kv[0]))]
	for idx, item in enumerate(items or [], start=1):
		if isinstance(item, dict):
			out.append({
				"step_id": step_id,
				"variable_key": str(item.get("variable_key") or idx),
				"source_field": item.get("source_field") or item.get("lead_field") or item.get("field_name") or item.get("field") or "",
				"static_fallback": item.get("static_fallback") or item.get("fallback") or "",
			})
		else:
			out.append({"step_id": step_id, "variable_key": str(idx), "source_field": str(item), "static_fallback": ""})
	return out


def _wa_step(step_id, src, **extra):
	header = "None"
	if src.get("attach_document_print"):
		header = "Document Print"
	elif src.get("custom_attachment"):
		header = "Field" if src.get("attach_from_field") else "File"
	return {"step_id": step_id, "step_type": "Send WhatsApp", "template": src.get("template"), "header_source": header,
		"header_file": src.get("attach") if header == "File" else None,
		"header_field": src.get("attach_from_field") if header == "Field" else None,
		"button_fields": src.get("button_fields"), "delivery_mode": "Direct", **extra}


def convert_lead(row: dict, mappings: list[dict]) -> dict:
	field = "status" if row.get("has_status", 1) or not row.get("has_workflow_state") else "workflow_state"
	out = _new({"trigger_type": "Field Change", "send_frequency": "Once per document", "skip_return_documents": 0})
	out["triggers"].append({"trigger_id": "t1", "label": f"{field.replace('_', ' ').title()} → {row.get('to_status')}",
		"field": field, "from_value": row.get("from_status") or "", "to_value": row.get("to_status") or ""})
	if row.get("recipient_list"):
		out["steps"].append({"step_id": "list", "step_type": "Add to Recipient List", "recipient_list": row["recipient_list"]})
	bulk = row.get("dispatch_mode") == "Bulk Broadcast Campaign"
	wa = _wa_step("wa", row, delivery_mode="Bulk campaign" if bulk else "Direct")
	if bulk:
		for k in ("bulk_campaign", "target_bulk_message", "bulk_schedule_type", "bulk_scheduled_time", "bulk_delay_minutes", "use_batched_enqueue"):
			wa[k] = row.get(k)
		wa["recipient_list"] = row.get("recipient_list")
	out["steps"].append(wa)
	out["mappings"] = _mapping_rows("wa", mappings)
	if not row.get("to_status"):
		out["problems"].append("no To status")
	if not row.get("template"):
		out["problems"].append("no template")
	return out


def convert_notification(row: dict, mappings: list[dict]) -> dict:
	ntype, event = row.get("notification_type"), row.get("doctype_event")
	values = {"send_frequency": "Once per day", "phone_field": row.get("field_name"), "email_field": row.get("email_field_name"),
		"skip_return_documents": 0}
	scheduler_problem = False
	if ntype == "Scheduler Event":
		values.update(trigger_type="Schedule", schedule_frequency=row.get("event_frequency"))
		scheduler_problem = not row.get("condition")
	elif event in ("Days Before", "Days After"):
		values.update(trigger_type="Date Based", date_field=row.get("date_changed"), offset_days=int(row.get("days_in_advance") or 0),
			offset_direction="Before" if event == "Days Before" else "After")
	else:
		values.update(trigger_type="Document Event", doc_event={"Before Delete": "On Trash"}.get(event, event))
	out = _new(values)
	if scheduler_problem:
		out["problems"].append("Scheduler rule without condition script — legacy never sent")
	if row.get("send_whatsapp"):
		if not row.get("template"):
			out["problems"].append("WhatsApp template is required")
		out["steps"].append(_wa_step("wa", row))
		out["mappings"] = _mapping_rows("wa", row.get("template_variables"))
	if row.get("send_email"):
		out["steps"].append({"step_id": "email", "step_type": "Send Email", "email_subject": row.get("email_subject"),
			"email_body": row.get("email_body"), "attach_print": row.get("attach_document_print") or 0})
	if row.get("send_sms"):
		out["steps"].append({"step_id": "sms", "step_type": "Send SMS", "sms_message": row.get("sms_message")})
	if not out["steps"]:
		out["problems"].append("no channel selected")
	elif row.get("set_property_after_alert") and row.get("property_value") is not None:
		out["steps"].append({"step_id": "update", "step_type": "Update Field", "update_field": row["set_property_after_alert"],
			"update_value": row.get("property_value")})
	return out


def convert_order(row: dict, mappings: list[dict]) -> dict:
	out = _new({"trigger_type": "Field Change", "send_frequency": "Every time",
		"skip_return_documents": 0 if row.get("include_return_notes") else 1})
	try:
		canvas = json.loads(row.get("canvas_data") or "{}") if isinstance(row.get("canvas_data"), str) else (row.get("canvas_data") or {})
	except ValueError:
		out["problems"].append("canvas is not valid JSON")
		return out
	nodes = {n["id"]: n for n in canvas.get("nodes", []) if isinstance(n, dict) and n.get("id")}
	adj: dict = {}
	for e in canvas.get("edges", []):
		if isinstance(e, dict) and e.get("from") in nodes:
			if e.get("to") not in nodes:
				out["problems"].append(f"edge {e.get('from')}→{e.get('to')} points to a missing node")
				continue
			adj.setdefault(e["from"], []).append(e)
	used: dict[str, int] = {}

	def new_id(base):
		used[base] = used.get(base, 0) + 1
		return base if used[base] == 1 else f"{base}-{used[base]}"

	def emit(node_id, trigger_id, parent, branch, depth, path):
		if node_id in path:
			out["problems"].append(f"cycle at node {node_id}")
			return
		node = nodes[node_id]
		ntype, data = node.get("type"), node.get("data") or {}
		base = dict(trigger_id=trigger_id if not parent else None, parent_step_id=parent, branch=branch)
		if ntype == "condition":
			if depth >= MAX_DEPTH:
				out["problems"].append(f"If/Else deeper than {MAX_DEPTH} at node {node_id}")
				return
			sid = new_id(node_id)
			out["steps"].append({"step_id": sid, "step_type": "If/Else", "condition_logic": OP_LOGIC.get(str(data.get("logic", "AND")).upper(), "All"),
				"condition_rules": json.dumps(data.get("rules") or []), **base})
			for e in adj.get(node_id, []):
				b = "Yes" if str(e.get("branch", "")).lower() == "true" else "No" if str(e.get("branch", "")).lower() == "false" else None
				if b:
					emit(e["to"], trigger_id, sid, b, depth + 1, path | {node_id})
			return
		if ntype == "channel":
			if data.get("field_name"):
				out["values"].setdefault("phone_field", data.get("field_name"))
			if data.get("whatsapp_account"):
				out["values"].setdefault("whatsapp_account", data.get("whatsapp_account"))
			first = True
			for flag, stype, suffix in (("send_whatsapp", "Send WhatsApp", ""), ("send_email", "Send Email", "-email"), ("send_sms", "Send SMS", "-sms")):
				if not data.get(flag):
					continue
				sid = new_id(node_id if first else node_id + suffix)
				if stype == "Send WhatsApp":
					if not data.get("template"):
						out["problems"].append("WhatsApp template is required")
					st = _wa_step(sid, data, **base)
					out["mappings"] += _mapping_rows(sid, data.get("variable_mappings") or data.get("template_variables"))
				elif stype == "Send Email":
					st = {"step_id": sid, "step_type": stype, "email_subject": data.get("email_subject"), "email_body": data.get("email_body"), **base}
					out["values"].setdefault("email_field", data.get("email_field_name"))
				else:
					st = {"step_id": sid, "step_type": stype, "sms_message": data.get("sms_message"), **base}
					out["values"].setdefault("sms_account", data.get("sms_account"))
				out["steps"].append(st)
				first = False
			if first:
				out["problems"].append(f"channel node {node_id} has no channel enabled")
		elif ntype == "property":
			if data.get("set_property_after_alert") and data.get("set_property_after_alert") != "None":
				out["steps"].append({"step_id": new_id(node_id), "step_type": "Update Field", "update_field": data["set_property_after_alert"],
					"update_value": data.get("property_value"), **base})
		else:
			out["problems"].append(f"unsupported node type {ntype} at {node_id}")
		for e in adj.get(node_id, []):
			if not str(e.get("branch", "")).strip():
				emit(e["to"], trigger_id, parent, branch, depth, path | {node_id})

	for node in [n for n in nodes.values() if n.get("type") == "status"]:
		data = node.get("data") or {}
		field, target = data.get("watched_field"), data.get("target_value") or ""
		if target == "Assigned / Changed":
			target = ""
		if not field:
			out["problems"].append(f"status node {node['id']} has no watched field")
			continue
		if not target and field not in AWB_FIELDS and data.get("target_value") != "Assigned / Changed":
			out["problems"].append(f"status node {node['id']} has no target value (never fired before)")
		out["triggers"].append({"trigger_id": node["id"], "label": f"{field} → {target or 'any'}", "field": field,
			"from_value": data.get("from_value") or "", "to_value": target})
		before = len(out["steps"])
		for e in adj.get(node["id"], []):
			emit(e["to"], node["id"], None, None, 0, {node["id"]})
		if len(out["steps"]) == before:
			out["problems"].append(f"status node {node['id']} leads to no steps")
	if not out["triggers"]:
		out["problems"].append("no status triggers")
	return out


def convert_row(row: dict, mappings: list[dict]) -> dict:
	t = row.get("notification_type")
	if t == "Lead Status Event":
		return convert_lead(row, mappings)
	if t == "Order Workflow Event":
		return convert_order(row, mappings)
	return convert_notification(row, mappings)
