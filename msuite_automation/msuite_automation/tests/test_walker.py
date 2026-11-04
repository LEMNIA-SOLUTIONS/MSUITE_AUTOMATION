import itertools
import unittest

from msuite_automation.msuite_automation.constants import WAIT_SIGNAL
from msuite_automation.msuite_automation.engine.graph import Graph
from msuite_automation.msuite_automation.engine.walker import advance, start_paths


def T(tid):
	return {"trigger_id": tid, "reference_doctype": "Sales Order"}


def S(sid, step_type="Send SMS", x=0):
	return {"step_id": sid, "step_type": step_type, "pos_x": x}


def E(a, b, h=""):
	return {"source_id": a, "source_handle": h, "target_id": b}


def run(graph, trigger="t1", handles=None, waits=(), paths=None):
	"""Walk; returns (executed node order, paths). handles: {node: handle}; waits: nodes that park."""
	order, counter = [], itertools.count(2)

	def execute(path, node):
		order.append(node)
		if node in waits:
			return WAIT_SIGNAL
		return (handles or {}).get(node, "")

	paths = advance(graph, paths or start_paths(trigger), execute, lambda: f"p{next(counter)}")
	return order, paths


class TestWalker(unittest.TestCase):
	def test_linear_and_if_else(self):
		g = Graph([T("t1")], [S("i", "If/Else"), S("a"), S("b"), S("c")],
			[E("t1", "i"), E("i", "a", "yes"), E("i", "b", "no"), E("a", "c"), E("b", "c")])
		order, paths = run(g, handles={"i": "no"})
		self.assertEqual(order, ["i", "b", "c"])
		self.assertEqual({p["status"] for p in paths}, {"Ended"})

	def test_fan_out_runs_branches_in_position_order(self):
		g = Graph([T("t1")], [S("left", x=0), S("right", x=500), S("l2")],
			[E("t1", "right"), E("t1", "left"), E("left", "l2")])
		order, paths = run(g)
		self.assertEqual(order, ["left", "l2", "right"])  # left branch first, depth-first
		self.assertEqual(len(paths), 2)

	def test_join_runs_once_after_all_branches(self):
		g = Graph([T("t1")], [S("a", x=0), S("b", x=100), S("j"), S("k")],
			[E("t1", "a"), E("t1", "b"), E("a", "j"), E("b", "j"), E("j", "k")])
		order, _ = run(g)
		self.assertEqual(order, ["a", "b", "j", "k"])
		self.assertEqual(order.count("j"), 1)

	def test_join_waits_for_waiting_path(self):
		g = Graph([T("t1")], [S("wa", x=0), S("mail", x=100), S("wait", "Wait"), S("j")],
			[E("t1", "wa"), E("t1", "mail"), E("wa", "j"), E("mail", "wait"), E("wait", "j")])
		order, paths = run(g, waits={"wait"})
		self.assertEqual(order, ["wa", "mail", "wait"])  # join not run yet
		by = {p["node_id"]: p["status"] for p in paths}
		self.assertEqual(by["j"], "At join")
		self.assertEqual(by["wait"], "Waiting")
		# resume the waiting path: it continues past the wait, the join now fires once
		for p in paths:
			if p["status"] == "Waiting":
				p["status"] = "Running"
		order2, paths2 = run(g, paths=paths)
		self.assertEqual(order2, ["j"])
		self.assertEqual({p["status"] for p in paths2}, {"Ended"})

	def test_join_released_when_other_branch_ends(self):
		g = Graph([T("t1")], [S("a", x=0), S("i", "If/Else", x=100), S("b"), S("j")],
			[E("t1", "a"), E("t1", "i"), E("i", "b", "yes"), E("a", "j"), E("b", "j")])
		order, paths = run(g, handles={"i": "no"})  # "no" has no wire → that path ends
		self.assertEqual(order, ["a", "i", "j"])
		self.assertEqual({p["status"] for p in paths}, {"Ended"})

	def test_trigger_with_no_wires_ends(self):
		order, paths = run(Graph([T("t1")], [], []))
		self.assertEqual(order, [])
		self.assertEqual(paths[0]["status"], "Ended")

	def test_loop_on_a_path_ends_it_with_an_error(self):
		# validation rejects this graph; the walker must still stop
		g = Graph([T("t1")], [S("a"), S("b")], [E("t1", "a"), E("a", "b"), E("b", "a")])
		order, paths = run(g)
		self.assertEqual(order, ["a", "b"])
		self.assertEqual(paths[0]["status"], "Ended")
		self.assertIn("Loop: a", paths[0]["error"])

	def test_step_limit(self):
		g = Graph([T("t1")], [S("a")], [E("t1", "a")])
		with self.assertRaises(RuntimeError):
			advance(g, start_paths("t1"), lambda p, n: "", lambda: "px", limit=0)
