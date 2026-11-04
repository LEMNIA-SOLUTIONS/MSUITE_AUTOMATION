import json
import unittest

from msuite_automation.msuite_automation.constants import WAIT_EVENT
from msuite_automation.msuite_automation.engine.graph import Graph, handles_for, validate_graph


def T(tid, dt="Sales Order", **kw):
	return {"trigger_id": tid, "trigger_type": "Field Change", "reference_doctype": dt, "field": "status", **kw}


def S(sid, step_type="Send SMS", **kw):
	base = {"step_id": sid, "step_type": step_type}
	if step_type == "Send SMS":
		base["sms_message"] = "hi"
	if step_type == "Wait":
		base.update(wait_value=1, wait_unit="Days")
	if step_type == WAIT_EVENT:
		base.update(wait_value=1, wait_unit="Days")
	return {**base, **kw}


def E(src, dst, handle=""):
	return {"source_id": src, "source_handle": handle, "target_id": dst}


class TestGraphWaitEvent(unittest.TestCase):
	def test_handles_wait_event(self):
		step = S("we", WAIT_EVENT)
		self.assertEqual(handles_for(step, False), ["event", "timeout"])

	def test_validation_timeout_must_be_greater_than_zero(self):
		step = S("we", WAIT_EVENT, wait_value=0, condition_rules=json.dumps([{"field": "status", "value": "Closed"}]))
		errs = validate_graph([T("t1")], [step], [E("t1", "we")])
		self.assertTrue(any("timeout must be greater than 0" in e for e in errs), errs)

	def test_validation_no_check_doctype_requires_rules(self):
		step = S("we", WAIT_EVENT, wait_value=1, condition_rules="[]")
		errs = validate_graph([T("t1")], [step], [E("t1", "we")])
		self.assertTrue(any("at least one rule is required" in e for e in errs), errs)

		step_none = S("we", WAIT_EVENT, wait_value=1, condition_rules=None)
		errs_none = validate_graph([T("t1")], [step_none], [E("t1", "we")])
		self.assertTrue(any("at least one rule is required" in e for e in errs_none), errs_none)

	def test_validation_check_doctype_requires_link_field(self):
		step = S("we", WAIT_EVENT, wait_value=1, check_doctype="Feedback", check_link_field="")
		errs = validate_graph([T("t1")], [step], [E("t1", "we")])
		self.assertTrue(any("link field is required" in e for e in errs), errs)

	def test_valid_step_same_doc_has_no_errors(self):
		step = S("we", WAIT_EVENT, wait_value=2, wait_unit="Hours",
			condition_rules=json.dumps([{"field": "status", "value": "Delivered"}]))
		edges = [E("t1", "we"), E("we", "s1", "event"), E("we", "s2", "timeout")]
		errs = validate_graph([T("t1")], [step, S("s1"), S("s2")], edges)
		self.assertEqual(errs, [])

	def test_valid_step_linked_mode_has_no_errors(self):
		step = S("we", WAIT_EVENT, wait_value=3, wait_unit="Days",
			check_doctype="Feedback", check_link_field="sales_order")
		edges = [E("t1", "we"), E("we", "s1", "event"), E("we", "s2", "timeout")]
		errs = validate_graph([T("t1")], [step, S("s1"), S("s2")], edges)
		self.assertEqual(errs, [])

	def test_wire_from_empty_handle_is_rejected(self):
		step = S("we", WAIT_EVENT, wait_value=1, check_doctype="Feedback", check_link_field="sales_order")
		edges = [E("t1", "we"), E("we", "s1", "")]
		errs = validate_graph([T("t1")], [step, S("s1")], edges)
		self.assertTrue(any("node we has no output ''" in e for e in errs), errs)
