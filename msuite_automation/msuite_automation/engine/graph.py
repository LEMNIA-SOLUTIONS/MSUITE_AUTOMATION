"""Pure graph helpers for the automation canvas. Nodes are trigger rows and
step rows, wires are Automation Edge rows. No frappe imports, so every rule
here is unit-testable and shared by the controller, executor and preview."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from msuite_core.constants import (
	IG_CONSENT_DOCTYPE,
	IG_CONSENT_IMAGE_SOURCE_POST,
	MESSENGER_BUTTON_TITLE_MAX,
	MESSENGER_TEMPLATE_SUBTITLE_MAX,
	MESSENGER_TEMPLATE_TITLE_MAX,
	SOCIAL_MENTION_DOCTYPE,
)
from msuite_automation.msuite_automation.constants import IG_CONSENT_MIN_EXPIRY_DAYS
from msuite_automation.msuite_automation.constants import (
	ACTION_TYPES,
	IF_ELSE,
	SEND_IG_CONSENT_TEMPLATE,
	SEND_IG_PUBLIC_REPLY,
	SEND_WHATSAPP,
	SWITCH,
	UPDATE_FIELD,
	WAIT,
	WAIT_EVENT,
)


# Consent-template step validation tables: (step field, label used in the message[, max length]).
_CONSENT_REQUIRED_FIELDS = (
	("ig_template_title", "template title"),
	("ig_yes_label", "yes label"),
	("ig_no_label", "no label"),
	("ig_view_label", "view label"),
)
_CONSENT_LENGTH_LIMITS = (
	("ig_template_title", "template title", MESSENGER_TEMPLATE_TITLE_MAX),
	("ig_template_subtitle", "template subtitle", MESSENGER_TEMPLATE_SUBTITLE_MAX),
	("ig_yes_label", "yes label", MESSENGER_BUTTON_TITLE_MAX),
	("ig_no_label", "no label", MESSENGER_BUTTON_TITLE_MAX),
	("ig_view_label", "view label", MESSENGER_BUTTON_TITLE_MAX),
)


def g(row, key, default=None):
	val = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
	return default if val is None else val


def switch_cases(step) -> list[str]:
	raw = g(step, "switch_cases") or "[]"
	try:
		val = json.loads(raw) if isinstance(raw, str) else raw
	except ValueError:
		return []
	return [str(v) for v in val] if isinstance(val, list) else []


def handles_for(node, is_trigger: bool) -> list[str]:
	if is_trigger:
		return [""]
	t = g(node, "step_type")
	if t == IF_ELSE:
		return ["yes", "no"]
	if t == SWITCH:
		return [f"case_{i}" for i in range(1, len(switch_cases(node)) + 1)] + ["otherwise"]
	if t == WAIT_EVENT:
		return ["event", "timeout"]
	return [""]


class Graph:
	def __init__(self, triggers, steps, edges):
		self.triggers = {g(t, "trigger_id"): t for t in triggers or []}
		self.steps = {g(s, "step_id"): s for s in steps or []}
		self.edges = [(g(e, "source_id"), g(e, "source_handle", "") or "", g(e, "target_id")) for e in edges or []]
		self._out: dict = {}
		self._in: dict = {}
		for src, handle, dst in self.edges:
			self._out.setdefault(src, {}).setdefault(handle, []).append(dst)
			self._in.setdefault(dst, []).append(src)
		self._reach: dict = {}

	def node(self, nid):
		return self.triggers.get(nid) or self.steps.get(nid)

	def is_trigger(self, nid) -> bool:
		return nid in self.triggers

	def _pos(self, nid):
		n = self.node(nid) or {}
		return (float(g(n, "pos_x", 0) or 0), float(g(n, "pos_y", 0) or 0), nid)

	def targets(self, nid, handle: str = "") -> list[str]:
		return sorted(set(self._out.get(nid, {}).get(handle or "", [])), key=self._pos)

	def successors(self, nid) -> set[str]:
		return {t for ts in self._out.get(nid, {}).values() for t in ts}

	def incoming(self, nid) -> list[str]:
		return list(self._in.get(nid, []))

	def is_join(self, nid) -> bool:
		return len(set(self._in.get(nid, []))) > 1

	def reachable(self, nid) -> set[str]:
		if nid not in self._reach:
			seen, stack = set(), list(self.successors(nid))
			while stack:
				n = stack.pop()
				if n in seen:
					continue
				seen.add(n)
				stack.extend(self.successors(n))
			self._reach[nid] = seen
		return self._reach[nid]

	def reaching_doctypes(self, nid) -> set[str]:
		return {g(t, "reference_doctype") for tid, t in self.triggers.items()
			if g(t, "reference_doctype") and (tid == nid or nid in self.reachable(tid))}

	def has_cycle(self) -> bool:
		WHITE, GREY, BLACK = 0, 1, 2
		color = {n: WHITE for n in [*self.triggers, *self.steps]}
		for start in list(color):
			if color[start] != WHITE:
				continue
			stack = [(start, iter(self.successors(start)))]
			color[start] = GREY
			while stack:
				node, it = stack[-1]
				nxt = next(it, None)
				if nxt is None:
					color[node] = BLACK
					stack.pop()
				elif color.get(nxt, WHITE) == GREY:
					return True
				elif color.get(nxt, WHITE) == WHITE:
					color[nxt] = GREY
					stack.append((nxt, iter(self.successors(nxt))))
		return False


def validate_graph(triggers, steps, edges) -> list[str]:
	errors: list[str] = []
	seen: set = set()
	for t in triggers or []:
		nid = g(t, "trigger_id")
		if nid in seen:
			errors.append(f"Duplicate node id: {nid}")
		seen.add(nid)
	for s in steps or []:
		nid = g(s, "step_id")
		if nid in seen:
			errors.append(f"Duplicate node id: {nid}")
		seen.add(nid)
	graph = Graph(triggers, steps, edges)
	wires = set()
	for src, handle, dst in graph.edges:
		if graph.node(src) is None or graph.node(dst) is None:
			errors.append(f"Wire {src} → {dst} connects an unknown node {src if graph.node(src) is None else dst}")
			continue
		if graph.is_trigger(dst):
			errors.append(f"Wire {src} → {dst}: wires can't go into trigger {dst}")
		if handle not in handles_for(graph.node(src), graph.is_trigger(src)):
			errors.append(f"Wire {src} → {dst}: node {src} has no output '{handle}'")
		if (src, handle, dst) in wires:
			errors.append(f"Duplicate wire {src} → {dst}")
		wires.add((src, handle, dst))
	if graph.has_cycle():
		errors.append("Wires form a loop — a workflow can't go back to an earlier node")
	for st in steps or []:
		sid, stype = g(st, "step_id"), g(st, "step_type")
		if stype == SEND_WHATSAPP and not g(st, "template"):
			errors.append(f"Step {sid}: WhatsApp template is required")
		if stype == WAIT and int(g(st, "wait_value") or 0) <= 0:
			errors.append(f"Step {sid}: wait must be greater than 0")
		if stype == WAIT_EVENT:
			if int(g(st, "wait_value") or 0) <= 0:
				errors.append(f"Step {sid}: timeout must be greater than 0")
			rules_raw = g(st, "condition_rules")
			rules = []
			if rules_raw:
				try:
					rules = json.loads(rules_raw) if isinstance(rules_raw, str) else rules_raw
				except ValueError:
					errors.append(f"Step {sid}: rules are not valid JSON")
					rules = None
			if not g(st, "check_doctype"):
				valid_rules = [r for r in rules or [] if isinstance(r, dict) and r.get("field")] if isinstance(rules, list) else []
				if not valid_rules:
					errors.append(f"Step {sid}: at least one rule is required")
			else:
				if not g(st, "check_link_field"):
					errors.append(f"Step {sid}: link field is required")
		if stype == UPDATE_FIELD and not g(st, "update_field"):
			errors.append(f"Step {sid}: field to update is required")
		if stype == IF_ELSE and g(st, "condition_rules"):
			try:
				json.loads(g(st, "condition_rules"))
			except ValueError:
				errors.append(f"Step {sid}: rules are not valid JSON")
		if stype == SWITCH:
			cases = switch_cases(st)
			if not g(st, "switch_field"):
				errors.append(f"Step {sid}: Switch field is required")
			if not cases:
				errors.append(f"Step {sid}: add at least one case")
			for c in {c for c in cases if cases.count(c) > 1}:
				errors.append(f"Step {sid}: duplicate case '{c}'")
		if stype in (SEND_IG_PUBLIC_REPLY, SEND_IG_CONSENT_TEMPLATE):
			if not triggers or any(g(t, "reference_doctype") != SOCIAL_MENTION_DOCTYPE for t in triggers):
				errors.append(f"Step {sid}: trigger DocType must be {SOCIAL_MENTION_DOCTYPE}")
		if stype == SEND_IG_PUBLIC_REPLY:
			if not g(st, "ig_reply_text"):
				errors.append(f"Step {sid}: reply text is required")
			expiry = g(st, "ig_expiry_days")
			if expiry is not None and str(expiry).strip() != "":
				try:
					if int(expiry) < IG_CONSENT_MIN_EXPIRY_DAYS:
						errors.append(f"Step {sid}: expiry days must be at least {IG_CONSENT_MIN_EXPIRY_DAYS}")
				except ValueError:
					errors.append(f"Step {sid}: expiry days must be a number")
		if stype == SEND_IG_CONSENT_TEMPLATE:
			for field, desc in _CONSENT_REQUIRED_FIELDS:
				if not g(st, field):
					errors.append(f"Step {sid}: {desc} is required")
			for field, desc, limit in _CONSENT_LENGTH_LIMITS:
				if len(g(st, field) or "") > limit:
					errors.append(f"Step {sid}: {desc} exceeds {limit} characters")
			image_source = str(g(st, "ig_image_source") or "").strip()
			if image_source and image_source != IG_CONSENT_IMAGE_SOURCE_POST:
				parsed = urlparse(image_source)
				if parsed.scheme not in ("http", "https") or not parsed.netloc:
					errors.append(f"Step {sid}: image must be the post image or an http(s) URL")
	return errors


def validate_warnings(triggers, steps, edges) -> list[str]:
	"""Return non-blocking warnings for the automation graph.

	Args:
		triggers: Iterable of Automation Trigger rows or dicts.
		steps: Iterable of Automation Step rows or dicts.
		edges: Iterable of Automation Edge rows or tuples.

	Returns:
		list[str]: Warning messages.

	Raises:
		None.

	Side Effects:
		None.
	"""
	consent_steps = [st for st in steps or [] if g(st, "step_type") == SEND_IG_CONSENT_TEMPLATE]
	if not consent_steps:
		return []
	graph = Graph(triggers, steps, edges)
	return [
		f"Step {sid}: Send IG Consent Template is not downstream of a Wait for Event step checking {IG_CONSENT_DOCTYPE}"
		for sid in (g(st, "step_id") for st in consent_steps)
		if not any(
			g(other_st, "step_type") == WAIT_EVENT
			and g(other_st, "check_doctype") == IG_CONSENT_DOCTYPE
			and sid in graph.reachable(other_sid)
			for other_sid, other_st in graph.steps.items()
		)
	]


def validate_activation(triggers, steps, edges) -> list[str]:
	if not triggers:
		return ["Add at least one trigger"]
	graph = Graph(triggers, steps, edges)
	return [f"Trigger {tid} does not lead to any action" for tid in graph.triggers
		if not any(g(graph.steps.get(n) or {}, "step_type") in ACTION_TYPES for n in graph.reachable(tid))]
