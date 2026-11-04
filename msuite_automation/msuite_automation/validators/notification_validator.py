"""Messaging Automation workflow graph validator and trigger extractor.
Kept for legacy patches and workflow graph parsing."""

from __future__ import annotations

import json
import frappe
from frappe import _

PROTECTED_PROPERTY_FIELDS = {
	"docstatus",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"name",
	"amended_from",
	"naming_series",
	"idx",
	"total",
	"net_total",
	"grand_total",
	"rounded_total",
	"base_total",
	"base_net_total",
	"base_grand_total",
	"base_rounded_total",
	"outstanding_amount",
	"paid_amount",
	"total_commission",
	"taxes_and_charges",
	"total_taxes_and_charges",
	"base_total_taxes_and_charges",
}

BLOCKED_CONDITION_FIELDS = {"password", "api_secret", "auth_token", "api_key", "secret"}

ALLOWED_WATCHED_FIELDS = (
	"status",
	"carrier_tracking_status",
	"unicommerce_shipping_status",
	"unicommerce_status",
	"lr_no",
	"awb_number",
	"unicommerce_manifest_id",
)

ALLOWED_CONDITION_OPERATORS = (
	"=",
	"!=",
	">",
	"<",
	">=",
	"<=",
	"contains",
	"in",
	"is set",
	"is not set",
)

ALLOWED_NODE_TYPES = ("source", "status", "condition", "channel", "property")
MAX_WORKFLOW_NODES = 100
MAX_WORKFLOW_EDGES = 200


def _validate_set_property_field(reference_doctype: str, fieldname: str) -> None:
	if not fieldname or fieldname == "None":
		return
	if fieldname in PROTECTED_PROPERTY_FIELDS:
		frappe.throw(
			_("Cannot modify protected or system field '{0}' on DocType {1}").format(fieldname, reference_doctype),
			frappe.ValidationError,
		)
	try:
		meta = frappe.get_meta(reference_doctype)
		df = meta.get_field(fieldname) if meta else None
	except Exception:
		return
	if not df:
		frappe.throw(_("Field {0} not found on DocType {1}").format(fieldname, reference_doctype), frappe.ValidationError)


def _validate_condition_field(reference_doctype: str, fieldname: str) -> None:
	if not fieldname:
		return
	target_dt = reference_doctype
	actual_field = fieldname
	if fieldname.startswith("sales_order."):
		if reference_doctype != "Delivery Note":
			frappe.throw(
				_("Prefix 'sales_order.' is only valid for Delivery Note workflows, not {0}").format(reference_doctype),
				frappe.ValidationError,
			)
		target_dt = "Sales Order"
		actual_field = fieldname[len("sales_order."):]
	if actual_field in BLOCKED_CONDITION_FIELDS:
		frappe.throw(
			_("Field '{0}' cannot be used in condition nodes").format(fieldname),
			frappe.ValidationError,
		)
	try:
		meta = frappe.get_meta(target_dt)
		df = meta.get_field(actual_field) if meta else None
	except Exception:
		return
	if df:
		if getattr(df, "fieldtype", None) == "Password":
			frappe.throw(
				_("Password fields cannot be used in condition nodes: {0}").format(fieldname),
				frappe.ValidationError,
			)
	else:
		standard_fields = {
			"name",
			"owner",
			"creation",
			"modified",
			"modified_by",
			"docstatus",
			"status",
			"carrier_tracking_status",
			"unicommerce_shipping_status",
			"unicommerce_status",
			"lr_no",
			"awb_number",
			"unicommerce_manifest_id",
			"delivery_attempts",
			"is_return",
			"grand_total",
			"net_total",
			"total",
			"rounded_total",
			"outstanding_amount",
		}
		if actual_field not in standard_fields:
			frappe.throw(
				_("Field '{0}' not found on DocType {1}").format(fieldname, target_dt),
				frappe.ValidationError,
			)


def validate_workflow_graph(canvas_data, reference_doctype: str | None = None) -> dict:
	"""Validate workflow graph rules:
	- exactly one source node
	- known node types: source, status, condition, channel, property
	- edges reference existing nodes
	- status nodes have watched_field in allowed list and valid target_value
	- condition node operators are valid and branch edges specify true/false
	- property nodes cannot modify protected/system fields and field exists
	- status nodes can only have incoming edges from source
	- graph sizes are within reasonable bounds
	"""
	if not canvas_data:
		frappe.throw(_("Canvas data is required"), frappe.ValidationError)

	if isinstance(canvas_data, str):
		try:
			data = json.loads(canvas_data)
		except Exception:
			frappe.throw(_("Canvas data must be valid JSON"), frappe.ValidationError)
	elif isinstance(canvas_data, dict):
		data = canvas_data
	else:
		frappe.throw(_("Canvas data must be a JSON object"), frappe.ValidationError)

	nodes = data.get("nodes")
	edges = data.get("edges")
	if not isinstance(nodes, list) or not isinstance(edges, list):
		frappe.throw(_("Canvas data must contain 'nodes' and 'edges' lists"), frappe.ValidationError)

	if len(nodes) > MAX_WORKFLOW_NODES:
		frappe.throw(_("Workflow graph cannot exceed {0} nodes").format(MAX_WORKFLOW_NODES), frappe.ValidationError)
	if len(edges) > MAX_WORKFLOW_EDGES:
		frappe.throw(_("Workflow graph cannot exceed {0} edges").format(MAX_WORKFLOW_EDGES), frappe.ValidationError)

	source_nodes = [n for n in nodes if n.get("type") == "source"]
	if len(source_nodes) != 1:
		frappe.throw(_("Workflow graph must have exactly one source node"), frappe.ValidationError)

	if reference_doctype and source_nodes[0].get("data", {}).get("reference_doctype"):
		src_dt = source_nodes[0]["data"]["reference_doctype"]
		if src_dt != reference_doctype:
			frappe.throw(
				_("Source node reference_doctype '{0}' does not match workflow reference_doctype '{1}'").format(
					src_dt, reference_doctype
				),
				frappe.ValidationError,
			)

	node_ids = set()
	for node in nodes:
		nid = node.get("id")
		if not nid:
			frappe.throw(_("All nodes must have an 'id'"), frappe.ValidationError)
		if nid in node_ids:
			frappe.throw(_("Duplicate node id: {0}").format(nid), frappe.ValidationError)
		node_ids.add(nid)

		ntype = node.get("type")
		if ntype not in ALLOWED_NODE_TYPES:
			frappe.throw(_("Unknown node type: {0}").format(ntype), frappe.ValidationError)

		ndata = node.get("data", {})
		if not isinstance(ndata, dict):
			ndata = {}

		if ntype == "status":
			wf = ndata.get("watched_field")
			if not wf or wf not in ALLOWED_WATCHED_FIELDS:
				frappe.throw(
					_("Status node {0} has invalid watched_field '{1}'. Allowed: {2}").format(
						nid, wf, ", ".join(ALLOWED_WATCHED_FIELDS)
					),
					frappe.ValidationError,
				)
			tv = ndata.get("target_value")
			if wf not in ("lr_no", "awb_number", "unicommerce_manifest_id"):
				if tv is None or str(tv).strip() == "":
					frappe.throw(
						_("Status node {0} must have a target_value for watched_field '{1}'").format(nid, wf),
						frappe.ValidationError,
					)

		elif ntype == "condition":
			logic = ndata.get("logic", "AND")
			if logic not in ("AND", "OR"):
				frappe.throw(_("Condition node {0} logic must be 'AND' or 'OR'").format(nid), frappe.ValidationError)
			rules = ndata.get("rules", [])
			if not isinstance(rules, list):
				frappe.throw(_("Condition node {0} rules must be a list").format(nid), frappe.ValidationError)
			for r in rules:
				if not isinstance(r, dict):
					continue
				op = r.get("operator")
				if op not in ALLOWED_CONDITION_OPERATORS:
					frappe.throw(
						_("Condition node {0} has invalid operator '{1}'. Allowed: {2}").format(
							nid, op, ", ".join(ALLOWED_CONDITION_OPERATORS)
						),
						frappe.ValidationError,
					)
				fld = r.get("field")
				if not fld or not str(fld).strip():
					frappe.throw(
						_("Condition node '{0}' contains a rule with no field specified").format(nid),
						frappe.ValidationError,
					)
				ref_dt = reference_doctype or (source_nodes[0].get("data", {}).get("reference_doctype") if source_nodes else None)
				if ref_dt:
					_validate_condition_field(ref_dt, fld)

		elif ntype == "property":
			prop_field = ndata.get("set_property_after_alert")
			if prop_field and prop_field != "None":
				ref_dt = reference_doctype or (source_nodes[0].get("data", {}).get("reference_doctype") if source_nodes else None)
				if ref_dt:
					_validate_set_property_field(ref_dt, prop_field)

	node_type_map = {n["id"]: n.get("type") for n in nodes if isinstance(n, dict) and "id" in n}
	cond_node_ids = {n["id"] for n in nodes if n.get("type") == "condition"}
	for edge in edges:
		f = edge.get("from")
		t = edge.get("to")
		if f not in node_ids or t not in node_ids:
			frappe.throw(_("Edge connects non-existent node: {0} -> {1}").format(f, t), frappe.ValidationError)
		if node_type_map.get(t) == "status" and node_type_map.get(f) != "source":
			frappe.throw(
				_("Status node {0} can only have incoming edges from a source node").format(t),
				frappe.ValidationError,
			)
		if f in cond_node_ids:
			b = str(edge.get("branch", "")).lower()
			if b not in ("true", "false"):
				frappe.throw(
					_("Edge leaving condition node {0} must specify branch as 'true' or 'false'").format(f),
					frappe.ValidationError,
				)

	return data


def extract_workflow_triggers(canvas_data) -> list[dict]:
	"""Extract trigger points (status nodes reachable from the single source node).
	Returns list of dicts: [{'node_id': ..., 'watched_field': ..., 'from_value': ..., 'target_value': ...}]
	"""
	if not canvas_data:
		return []

	if isinstance(canvas_data, str):
		try:
			data = json.loads(canvas_data)
		except Exception:
			return []
	elif isinstance(canvas_data, dict):
		data = canvas_data
	else:
		return []

	nodes = data.get("nodes", [])
	edges = data.get("edges", [])
	node_map = {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}

	source_nodes = [n for n in nodes if isinstance(n, dict) and n.get("type") == "source"]
	if not source_nodes:
		return []
	source_id = source_nodes[0].get("id")

	adj: dict[str, list[str]] = {}
	for edge in edges:
		if isinstance(edge, dict) and "from" in edge and "to" in edge:
			adj.setdefault(edge["from"], []).append(edge["to"])

	visited = {source_id}
	visited_order = [source_id]
	queue = [source_id]
	while queue:
		curr = queue.pop(0)
		for nxt in adj.get(curr, []):
			if nxt in node_map and nxt not in visited:
				visited.add(nxt)
				visited_order.append(nxt)
				queue.append(nxt)

	triggers = []
	for nid in visited_order:
		n = node_map.get(nid)
		if n and n.get("type") == "status":
			d = n.get("data", {})
			triggers.append({
				"node_id": nid,
				"watched_field": d.get("watched_field"),
				"from_value": d.get("from_value", ""),
				"target_value": d.get("target_value", ""),
			})

	return triggers
