import unittest

from msuite_automation.msuite_automation.migration.graph_convert import tree_to_graph

PARENT = {"trigger_type": "Field Change", "reference_doctype": "Sales Order", "filter_logic": "Any", "condition": "doc.x",
	"phone_field": "contact_mobile", "email_field": "contact_email"}


def st(sid, idx, step_type="Send SMS", tid="", parent=None, branch=None):
	return {"step_id": sid, "idx": idx, "step_type": step_type, "trigger_id": tid, "parent_step_id": parent, "branch": branch}


def edges(out):
	return sorted((e["source_id"], e["source_handle"], e["target_id"]) for e in out["edges"])


class TestTreeToGraph(unittest.TestCase):
	def test_field_change_triggers_get_parent_kind_fields_and_filters(self):
		out = tree_to_graph(PARENT, [{"trigger_id": "t1", "field": "status", "to_value": "Shipped"}],
			[{"field": "territory", "operator": "=", "value": "India"}], [st("a", 1, tid="t1")],
			[{"step_id": "a", "variable_key": "1", "source_field": "customer_name"}])
		t = out["triggers"][0]
		self.assertEqual((t["trigger_type"], t["reference_doctype"], t["filter_logic"], t["condition"]),
			("Field Change", "Sales Order", "Any", "doc.x"))
		self.assertEqual(out["filters"][0]["trigger_id"], "t1")
		self.assertEqual(edges(out), [("t1", "", "a")])
		self.assertEqual(out["mappings"][0]["reference_doctype"], "Sales Order")
		self.assertEqual(out["recipients"], [{"reference_doctype": "Sales Order", "phone_field": "contact_mobile", "email_field": "contact_email"}])
		self.assertEqual(out["problems"], [])

	def test_document_event_parent_becomes_one_trigger(self):
		out = tree_to_graph({"trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Insert"}, [], [],
			[st("a", 1), st("b", 2)], [])
		self.assertEqual(len(out["triggers"]), 1)
		t = out["triggers"][0]
		self.assertEqual((t["trigger_type"], t["doc_event"]), ("Document Event", "After Insert"))
		self.assertEqual(edges(out), sorted([(t["trigger_id"], "", "a"), ("a", "", "b")]))

	def test_if_else_branches_and_tail_join(self):
		steps = [st("i", 1, "If/Else"), st("y", 2, parent="i", branch="Yes"), st("n", 3, parent="i", branch="No"), st("z", 4)]
		out = tree_to_graph({"trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}, [], [], steps, [])
		tid = out["triggers"][0]["trigger_id"]
		self.assertEqual(edges(out), sorted([(tid, "", "i"), ("i", "yes", "y"), ("i", "no", "n"), ("y", "", "z"), ("n", "", "z")]))

	def test_empty_branch_continues_to_next(self):
		steps = [st("i", 1, "If/Else"), st("y", 2, parent="i", branch="Yes"), st("z", 3)]
		out = tree_to_graph({"trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}, [], [], steps, [])
		self.assertIn(("i", "no", "z"), edges(out))

	def test_shared_steps_become_a_join(self):
		trig = [{"trigger_id": "t1", "field": "status", "to_value": "A"}, {"trigger_id": "t2", "field": "status", "to_value": "B"}]
		steps = [st("a", 1, tid="t1"), st("b", 2, tid="t2"), st("s", 3)]
		out = tree_to_graph(PARENT, trig, [], steps, [])
		self.assertEqual(edges(out), sorted([("t1", "", "a"), ("a", "", "s"), ("t2", "", "b"), ("b", "", "s")]))
		self.assertEqual(out["problems"], [])

	def test_shared_step_with_divergent_tails_is_a_problem(self):
		trig = [{"trigger_id": "t1", "field": "status", "to_value": "A"}, {"trigger_id": "t2", "field": "status", "to_value": "B"}]
		steps = [st("s", 1), st("x", 2, tid="t1"), st("y", 3, tid="t2")]
		out = tree_to_graph(PARENT, trig, [], steps, [])
		self.assertTrue(any("different next steps" in p for p in out["problems"]))

	def test_nested_if_else_tail(self):
		steps = [st("i", 1, "If/Else"), st("j", 2, "If/Else", parent="i", branch="Yes"), st("jy", 3, parent="j", branch="Yes"), st("z", 4)]
		out = tree_to_graph({"trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}, [], [], steps, [])
		e = edges(out)
		self.assertIn(("jy", "", "z"), e)   # deepest branch climbs to the step after the outer If/Else
		self.assertIn(("j", "no", "z"), e)
		self.assertIn(("i", "no", "z"), e)

	def test_if_else_shared_step_with_per_trigger_tails(self):
		trig = [{"trigger_id": "t1", "field": "status", "to_value": "A"}, {"trigger_id": "t2", "field": "status", "to_value": "B"}]
		steps = [st("s", 1, "If/Else"), st("x", 2, tid="t1"), st("y", 3, tid="t2")]
		out = tree_to_graph(PARENT, trig, [], steps, [])
		self.assertEqual(len(out["problems"]), 2)
		self.assertIn("Step s on branch 'no' is shared by several triggers but has different next steps (x, y) — review the wiring", out["problems"])
		self.assertIn("Step s on branch 'yes' is shared by several triggers but has different next steps (x, y) — review the wiring", out["problems"])

	def test_shared_step_continuing_for_t1_but_ending_for_t2(self):
		trig = [{"trigger_id": "t1", "field": "status", "to_value": "A"}, {"trigger_id": "t2", "field": "status", "to_value": "B"}]
		steps = [st("s", 1), st("x", 2, tid="t1")]
		out = tree_to_graph(PARENT, trig, [], steps, [])
		self.assertEqual(len(out["problems"]), 1)
		self.assertEqual(out["problems"][0], "Step s is shared by several triggers but has different next steps (end, x) — review the wiring")
