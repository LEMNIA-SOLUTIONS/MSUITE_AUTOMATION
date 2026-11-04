"""Pure conversion of a tree-shaped Messaging Automation (Phase 1–3 builder)
into the canvas graph: parent trigger fields move onto trigger rows, parent
filters are copied per trigger, parent/branch links become wires. Semantics
are preserved exactly; anything that can't be preserved is reported in
`problems` (the patch then saves the automation disabled)."""

from __future__ import annotations

from msuite_automation.msuite_automation.constants import FIELD_CHANGE, IF_ELSE

KIND_FIELDS = ("trigger_type", "reference_doctype", "doc_event", "date_field", "offset_days", "offset_direction",
	"schedule_frequency", "filter_logic", "condition")


def _triggers(parent: dict, triggers: list[dict]) -> list[dict]:
	kind = {k: parent.get(k) for k in KIND_FIELDS}
	kind["filter_logic"] = kind.get("filter_logic") or "All"
	if parent.get("trigger_type") == FIELD_CHANGE and triggers:
		return [{**t, **kind} for t in triggers]
	tid = (triggers[0].get("trigger_id") if triggers else None) or "t1"
	return [{"trigger_id": tid, "label": "", **kind}]


def tree_to_graph(parent: dict, triggers: list[dict], filters: list[dict], steps: list[dict], mappings: list[dict]) -> dict:
	out_triggers = _triggers(parent, triggers)
	problems: list[str] = []
	by_idx = sorted(steps, key=lambda s: s.get("idx") or 0)

	def top(tid):
		return [s for s in by_idx if not s.get("parent_step_id") and (not s.get("trigger_id") or s.get("trigger_id") == tid)]

	def kids(pid, branch):
		return [s for s in by_idx if s.get("parent_step_id") == pid and (s.get("branch") or "") == branch]

	edges: dict = {}  # (source, handle) -> set(targets)
	recorded: dict[tuple[str, str], set] = {}

	def wire(src, handle, dst):
		edges.setdefault((src, handle), set()).add(dst)

	def chain(seq, after, tid):
		for i, s in enumerate(seq):
			nxt = seq[i + 1]["step_id"] if i + 1 < len(seq) else after
			if s.get("step_type") == IF_ELSE:
				for branch in ("Yes", "No"):
					inner = kids(s["step_id"], branch)
					handle = branch.lower()
					if inner:
						wire(s["step_id"], handle, inner[0]["step_id"])
						recorded.setdefault((s["step_id"], handle), set()).add(inner[0]["step_id"])
						chain(inner, nxt, tid)
					else:
						if nxt:
							wire(s["step_id"], handle, nxt)
						recorded.setdefault((s["step_id"], handle), set()).add(nxt)
			else:
				if nxt:
					wire(s["step_id"], "", nxt)
				recorded.setdefault((s["step_id"], ""), set()).add(nxt)

	for t in out_triggers:
		seq = top(t["trigger_id"])
		if seq:
			wire(t["trigger_id"], "", seq[0]["step_id"])
			chain(seq, None, t["trigger_id"])

	for (src, handle), values in sorted(recorded.items()):
		if len(values) > 1 and src not in {t["trigger_id"] for t in out_triggers}:
			problems.append(
				f"Step {src}{' on branch ' + repr(handle) if handle else ''} is shared by several triggers but has different next steps ({', '.join(sorted(str(x) if x else 'end' for x in values))}) — review the wiring"
			)

	dt = parent.get("reference_doctype")
	return {
		"triggers": out_triggers,
		"filters": [{**f, "trigger_id": t["trigger_id"]} for t in out_triggers for f in filters],
		"edges": [{"source_id": s, "source_handle": h, "target_id": d} for (s, h), ds in edges.items() for d in sorted(ds)],
		"mappings": [{**m, "reference_doctype": m.get("reference_doctype") or dt} for m in mappings],
		"recipients": [{"reference_doctype": dt, "phone_field": parent.get("phone_field") or "", "email_field": parent.get("email_field") or ""}] if dt else [],
		"problems": problems,
	}
