import json
import unittest

from msuite_automation.msuite_automation.engine.graph import Graph, handles_for, validate_activation, validate_graph


def T(tid, dt="Sales Order", **kw):
	return {"trigger_id": tid, "trigger_type": "Field Change", "reference_doctype": dt, "field": "status", **kw}


def S(sid, step_type="Send SMS", **kw):
	base = {"step_id": sid, "step_type": step_type, "sms_message": "hi"}
	if step_type == "Send WhatsApp":
		base["template"] = "tpl"
	if step_type == "Wait":
		base.update(wait_value=1, wait_unit="Days")
	if step_type == "Update Field":
		base["update_field"] = "status"
	return {**base, **kw}


def E(src, dst, handle=""):
	return {"source_id": src, "source_handle": handle, "target_id": dst}


class TestGraph(unittest.TestCase):
	def test_targets_ordered_by_position_and_join(self):
		g = Graph([T("t1")], [S("a", pos_x=300, pos_y=0), S("b", pos_x=100, pos_y=0), S("j")],
			[E("t1", "a"), E("t1", "b"), E("a", "j"), E("b", "j")])
		self.assertEqual(g.targets("t1"), ["b", "a"])  # left to right
		self.assertTrue(g.is_join("j"))
		self.assertFalse(g.is_join("a"))
		self.assertEqual(g.reachable("t1"), {"a", "b", "j"})

	def test_handles(self):
		self.assertEqual(handles_for(T("t"), True), [""])
		self.assertEqual(handles_for(S("i", "If/Else"), False), ["yes", "no"])
		sw = S("s", "Switch", switch_field="territory", switch_cases=json.dumps(["India", "UAE"]))
		self.assertEqual(handles_for(sw, False), ["case_1", "case_2", "otherwise"])
		self.assertEqual(handles_for(S("w", "Wait"), False), [""])

	def test_reaching_doctypes(self):
		g = Graph([T("t1", "Sales Order"), T("t2", "Delivery Note")], [S("x"), S("y")],
			[E("t1", "x"), E("t2", "x"), E("x", "y")])
		self.assertEqual(g.reaching_doctypes("y"), {"Sales Order", "Delivery Note"})
		self.assertEqual(g.reaching_doctypes("t1"), {"Sales Order"})

	def test_cycle(self):
		self.assertTrue(Graph([T("t1")], [S("a"), S("b")], [E("t1", "a"), E("a", "b"), E("b", "a")]).has_cycle())
		self.assertFalse(Graph([T("t1")], [S("a"), S("b")], [E("t1", "a"), E("a", "b")]).has_cycle())


class TestValidateGraph(unittest.TestCase):
	def test_ok(self):
		steps = [S("i", "If/Else", condition_rules="[]"), S("w", "Send WhatsApp"), S("e", "Send Email", email_subject="x"), S("d", "Wait")]
		edges = [E("t1", "i"), E("i", "w", "yes"), E("i", "e", "no"), E("w", "d"), E("e", "d")]
		self.assertEqual(validate_graph([T("t1")], steps, edges), [])

	def test_step_with_legacy_trigger_id_is_not_a_duplicate(self):
		errs = validate_graph([T("t1")], [S("a", trigger_id="t1")], [E("t1", "a")])
		self.assertNotIn("Duplicate node id", "\n".join(errs))
		self.assertEqual(errs, [])

	def test_structural_errors(self):
		errs = validate_graph([T("t1"), T("t2")], [S("a"), S("a"), S("b")],
			[E("t1", "zz"), E("a", "t2"), E("t1", "a", "yes"), E("t1", "b"), E("t1", "b")])
		text = "\n".join(errs)
		self.assertIn("Duplicate node id: a", text)
		self.assertIn("unknown node zz", text)
		self.assertIn("into trigger t2", text)
		self.assertIn("output 'yes'", text)
		self.assertIn("Duplicate wire t1 → b", text)

	def test_loop_rejected(self):
		errs = validate_graph([T("t1")], [S("a"), S("b")], [E("t1", "a"), E("a", "b"), E("b", "a")])
		self.assertTrue(any("loop" in e for e in errs))

	def test_step_rules(self):
		errs = "\n".join(validate_graph([T("t1")], [
			S("w", "Send WhatsApp", template=""), S("d", "Wait", wait_value=0), S("u", "Update Field", update_field=""),
			S("s1", "Switch", switch_field="", switch_cases="[]"), S("s2", "Switch", switch_field="x", switch_cases='["A","A"]'),
			S("i", "If/Else", condition_rules="not json")], []))
		for msg in ("w: WhatsApp template is required", "d: wait must be greater than 0", "u: field to update is required",
				"s1: Switch field is required", "s1: add at least one case", "s2: duplicate case 'A'", "i: rules are not valid JSON"):
			self.assertIn(msg, errs)

	def test_activation(self):
		self.assertEqual(validate_activation([], [], []), ["Add at least one trigger"])
		errs = validate_activation([T("t1"), T("t2")], [S("a"), S("d", "Wait")], [E("t1", "a"), E("t2", "d")])
		self.assertEqual(errs, ["Trigger t2 does not lead to any action"])

	def test_date_based_trigger_offset_days(self):
		"""Validate offset_days on Date Based trigger: 1 to 10 inclusive, ignored on others."""
		def date_trigger(offset):
			return {"trigger_id": "t1", "trigger_type": "Date Based", "reference_doctype": "Sales Order",
				"date_field": "delivery_date", "offset_days": offset}

		valid_cases = [1, 10, "1", "10", 5]
		for val in valid_cases:
			errs = validate_graph([date_trigger(val)], [S("a")], [E("t1", "a")])
			self.assertNotIn("offset days must be a whole number", "\n".join(errs), f"Expected {val} to be valid")

		invalid_cases = [0, -1, 11, 1.5, "abc", None]
		for val in invalid_cases:
			errs = validate_graph([date_trigger(val)], [S("a")], [E("t1", "a")])
			self.assertTrue(any("offset days must be a whole number from 1 to 10" in e for e in errs),
				f"Expected {val} to be rejected, got: {errs}")

		# Ignored for other trigger types
		field_trigger = {"trigger_id": "t1", "trigger_type": "Field Change", "reference_doctype": "Sales Order",
			"field": "status", "offset_days": 0}
		self.assertEqual(validate_graph([field_trigger], [S("a")], [E("t1", "a")]), [])

	def test_wait_value_validation(self):
		"""Validate wait_value: must be whole number > 0, no upper cap."""
		for val in (0, -1):
			errs = validate_graph([T("t1")], [S("d", "Wait", wait_value=val)], [E("t1", "d")])
			self.assertTrue(any("wait must be greater than 0" in e for e in errs), f"Expected {val} to fail > 0 check")

		for val in (1.5, "1.5"):
			errs = validate_graph([T("t1")], [S("d", "Wait", wait_value=val)], [E("t1", "d")])
			self.assertTrue(any("wait must be a whole number" in e for e in errs), f"Expected {val} to fail whole number check")

		# Huge value is valid (no new upper cap)
		self.assertEqual(validate_graph([T("t1")], [S("d", "Wait", wait_value=1000000)], [E("t1", "d")]), [])

	def test_empty_if_else_rule_list_draft_vs_activation(self):
		"""Empty If/Else rules: warning on draft, blocking on activation."""
		from msuite_automation.msuite_automation.engine.graph import validate_warnings
		triggers = [T("t1")]
		steps = [S("i", "If/Else", condition_rules="[]"), S("a")]
		edges = [E("t1", "i"), E("i", "a", "yes")]

		# Draft allows saving (validate_graph has no errors)
		self.assertEqual(validate_graph(triggers, steps, edges), [])
		# Draft produces warning
		warnings = validate_warnings(triggers, steps, edges)
		self.assertTrue(any("i: add at least one rule" in w for w in warnings), f"Expected warning, got: {warnings}")

		# Activation is blocked
		act_errs = validate_activation(triggers, steps, edges)
		self.assertTrue(any("i: add at least one rule" in e for e in act_errs), f"Expected activation error, got: {act_errs}")

	def test_orphan_wait_draft_vs_activation(self):
		"""Orphan node: warning on draft, blocking on activation."""
		from msuite_automation.msuite_automation.engine.graph import validate_warnings
		triggers = [T("t1")]
		steps = [S("a"), S("d", "Wait", wait_value=1)]
		edges = [E("t1", "a")]  # d has no incoming wire

		# Draft allows saving
		self.assertEqual(validate_graph(triggers, steps, edges), [])
		warnings = validate_warnings(triggers, steps, edges)
		self.assertTrue(any("Step d is not connected to the flow" in w for w in warnings), f"Expected orphan warning, got: {warnings}")

		# Activation is blocked
		act_errs = validate_activation(triggers, steps, edges)
		self.assertTrue(any("Step d is not connected to the flow" in e for e in act_errs), f"Expected orphan activation error, got: {act_errs}")

	def test_orphan_with_chain_of_nodes(self):
		"""Orphan chain: all unreachable nodes warned on draft, blocked on activation."""
		from msuite_automation.msuite_automation.engine.graph import validate_warnings
		triggers = [T("t1")]
		steps = [S("a"), S("d1", "Wait", wait_value=1), S("d2", "Wait", wait_value=2)]
		edges = [E("t1", "a"), E("d1", "d2")]  # d1->d2 chain not reachable from t1

		warnings = validate_warnings(triggers, steps, edges)
		self.assertTrue(any("Step d1 is not connected to the flow" in w for w in warnings))
		self.assertTrue(any("Step d2 is not connected to the flow" in w for w in warnings))

		act_errs = validate_activation(triggers, steps, edges)
		self.assertTrue(any("Step d1 is not connected to the flow" in e for e in act_errs))
		self.assertTrue(any("Step d2 is not connected to the flow" in e for e in act_errs))
