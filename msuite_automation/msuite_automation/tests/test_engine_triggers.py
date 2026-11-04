import unittest
from unittest.mock import MagicMock, patch

import frappe

from msuite_automation.msuite_automation.engine import triggers as T

EX = "msuite_automation.msuite_automation.engine.executor"


def row(name, tid, field, frm, to):
	return [name, tid, field, frm, to]


class TestMatch(unittest.TestCase):
	def test_to_value_and_from_value(self):
		rows = [row("A", "t1", "status", "", "Won"), row("A", "t2", "status", "New", "Lost"), row("B", "t1", "status", "", "")]
		self.assertEqual(T.match(rows, {"status": {"old": "New", "new": "Won"}}), [("A", "t1", "Won", ""), ("B", "t1", "Won", "")])
		self.assertEqual(T.match(rows, {"status": {"old": "Open", "new": "Lost"}}), [("B", "t1", "Lost", "")])

	def test_blank_to_requires_non_empty(self):
		self.assertEqual(T.match([row("A", "t", "lr_no", "", "")], {"lr_no": {"old": "X", "new": ""}}), [])

	def test_awb_alias(self):
		self.assertEqual(T.match([row("A", "t", "lr_no", "", "")], {"awb_number": {"old": "", "new": "AWB1"}}), [("A", "t", "AWB1", "")])

	def test_failed_attempt_repeat(self):
		diff = {"carrier_tracking_status": {"old": "FAILED_ATTEMPT_1", "new": "FAILED_ATTEMPT", "delivery_attempts": 2}}
		self.assertEqual(T.match([row("A", "t", "carrier_tracking_status", "", "FAILED_ATTEMPT")], diff),
			[("A", "t", "FAILED_ATTEMPT", "2")])

	def test_from_value_case_insensitive(self):
		rows = [row("A", "t1", "status", "new", "Lost")]
		self.assertEqual(T.match(rows, {"status": {"old": "New", "new": "Lost"}}), [("A", "t1", "Lost", "")])


class TestBuildDiff(unittest.TestCase):
	def test_insert_and_update_and_cancel(self):
		doc = frappe._dict(status="Draft", lr_no="", docstatus=0)
		self.assertEqual(T.build_diff(doc, {"status", "lr_no"}, None), {"status": {"old": "", "new": "Draft"}})
		before = frappe._dict(status="Draft", lr_no="")
		doc2 = frappe._dict(status="To Deliver", lr_no="L1", docstatus=1)
		self.assertEqual(set(T.build_diff(doc2, {"status", "lr_no"}, before)), {"status", "lr_no"})
		cancelled = frappe._dict(status="Draft", docstatus=2)
		self.assertEqual(T.build_diff(cancelled, {"status"}, frappe._dict(status="Draft")), {"status": {"old": "", "new": "Cancelled"}})


class TestHotPath(unittest.TestCase):
	@patch.object(T, "get_watch_map", return_value={})
	@patch.object(T, "get_map", return_value={})
	def test_no_automation_zero_queries(self, _map, _wmap):
		doc = frappe.get_doc({"doctype": "ToDo", "description": "x"})
		with patch("frappe.db.sql") as sql:
			T.on_doc_event(doc, "on_update")
		sql.assert_not_called()

	@patch(f"{EX}.enqueue_run")
	def test_new_doc_fires_once(self, enqueue):
		m = {"CRM Lead": {"events": {}, "fields": [row("A", "t1", "status", "", "New")]}}
		doc = frappe._dict(doctype="CRM Lead", name="L1", status="New", flags=frappe._dict(in_insert=True))
		doc.get_doc_before_save = lambda: None
		with patch.object(T, "get_map", return_value=m):
			T.on_doc_event(doc, "after_insert")
			T.on_doc_event(doc, "on_update")
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(enqueue.call_args.kwargs["trigger_id"], "t1")

	@patch(f"{EX}.enqueue_run")
	def test_document_event(self, enqueue):
		m = {"ToDo": {"events": {"After Save": [["N1", "t1"]]}, "fields": []}}
		doc = frappe._dict(doctype="ToDo", name="T1", flags=frappe._dict())
		with patch.object(T, "get_map", return_value=m):
			T.on_doc_event(doc, "on_update")
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["name"], "N1")
		self.assertEqual(enqueue.call_args.kwargs["trigger_id"], "t1")

	@patch(f"{EX}.enqueue_run")
	def test_on_trash_fires_on_trash_automation(self, enqueue):
		m = {"ToDo": {"events": {"On Trash": [["N_TRASH", "t1"]]}, "fields": []}}
		doc = frappe._dict(doctype="ToDo", name="T1", flags=frappe._dict())
		doc.as_dict = lambda: {"doctype": "ToDo", "name": "T1"}
		with patch.object(T, "get_map", return_value=m):
			T.on_doc_event(doc, "on_trash")
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["name"], "N_TRASH")
		self.assertEqual(enqueue.call_args.kwargs["trigger_id"], "t1")

	@patch(f"{EX}.enqueue_run")
	def test_before_insert_passes_doc_dict(self, enqueue):
		m = {"ToDo": {"events": {"Before Insert": [["N_INSERT", "t1"]]}, "fields": []}}
		doc = frappe._dict(doctype="ToDo", name=None, description="task", flags=frappe._dict())
		doc.as_dict = lambda: {"doctype": "ToDo", "name": None, "description": "task"}
		with patch.object(T, "get_map", return_value=m):
			T.on_doc_event(doc, "before_insert")
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["name"], "N_INSERT")
		self.assertEqual(enqueue.call_args.kwargs["trigger_id"], "t1")
		self.assertEqual(enqueue.call_args.kwargs["doc_dict"], {"doctype": "ToDo", "name": None, "description": "task"})
