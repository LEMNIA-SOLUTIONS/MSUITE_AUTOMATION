"""AUT-103: the Update Field step saves through doc.save(), so the record's own rules apply, and its save never
starts other automations. Runs against the local test DB; no live calls."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_automation.msuite_automation.constants import AUTOMATION_WRITE_FLAG
from msuite_automation.msuite_automation.engine import steps, triggers


def _step(field="status", value="Closed"):
	return frappe._dict(step_type="Update Field", update_field=field, update_value=value)


class TestUpdateFieldFollowsRecordRules(IntegrationTestCase):
	def setUp(self):
		self.todo = frappe.get_doc({"doctype": "ToDo", "description": "ZZ aut103", "status": "Open"}).insert(ignore_permissions=True)
		self.ctx = frappe._dict(automation=frappe._dict(name="MA-X", send_frequency="Every time"), doc=self.todo)

	def tearDown(self):
		frappe.db.rollback()

	def test_valid_update_is_saved_and_reflected_in_memory(self):
		self.assertEqual(steps.execute(self.ctx, _step()), "updated")
		self.assertEqual(frappe.db.get_value("ToDo", self.todo.name, "status"), "Closed")
		self.assertEqual(self.todo.status, "Closed")

	def test_a_rule_that_rejects_the_value_fails_the_step_and_keeps_the_record(self):
		"""e.g. the Lead pipeline rule: New -> Follow-up Required is not a valid move."""
		reject = frappe.ValidationError("Transition 'New' → 'Follow-up Required' is not a valid Lead pipeline move.")
		with patch("frappe.desk.doctype.todo.todo.ToDo.validate", side_effect=reject):
			with self.assertRaises(frappe.ValidationError) as cm:
				steps.execute(self.ctx, _step())
		self.assertIn("not a valid Lead pipeline move", str(cm.exception))
		self.assertEqual(frappe.db.get_value("ToDo", self.todo.name, "status"), "Open")
		self.assertEqual(self.todo.status, "Open")

	def test_the_save_carries_the_loop_guard_flag(self):
		seen = []
		with patch.object(triggers, "on_doc_event", side_effect=lambda doc, method: seen.append(bool(doc.flags.get(AUTOMATION_WRITE_FLAG)))):
			steps.execute(self.ctx, _step())
		self.assertTrue(seen and all(seen), seen)

	def test_guarded_save_never_starts_automations(self):
		doc = frappe.get_doc("ToDo", self.todo.name)
		doc.flags[AUTOMATION_WRITE_FLAG] = True
		with patch.object(triggers, "get_map") as get_map, patch.object(triggers, "get_watch_map") as get_watch:
			for method in ("validate", "on_update", "after_insert"):
				triggers.on_doc_event(doc, method)
			get_map.assert_not_called()
			get_watch.assert_not_called()

	def test_unflagged_save_still_looks_up_automations(self):
		doc = frappe.get_doc("ToDo", self.todo.name)
		with patch.object(triggers, "get_map", return_value={}) as get_map, patch.object(triggers, "get_watch_map", return_value={}):
			triggers.on_doc_event(doc, "on_update")
			get_map.assert_called()


class TestRunStatusWithUpdateFieldErrors(IntegrationTestCase):
	"""A rejected Update Field value must show as a Failed run (so Recent runs and Retry show it)."""

	def _status(self, log):
		from unittest.mock import MagicMock

		from msuite_automation.msuite_automation.engine import executor

		run = frappe._dict(paths=[], status=None, log=None, sent_count=0, flags=frappe._dict(), save=MagicMock())
		run.set = lambda k, v: setattr(run, k, v)
		executor._persist(run, [{"path_id": "p1", "node_id": "u1", "status": "Completed", "executed": 1}], log)
		return run.status

	def test_an_update_field_error_fails_the_run(self):
		self.assertEqual(self._status([{"path_id": "p1", "node_id": "u1", "step_type": "Update Field", "result": "error", "error": "bad move"}]), "Failed")

	def test_an_update_field_error_fails_the_run_even_if_a_send_succeeded(self):
		log = [{"path_id": "p1", "node_id": "s1", "step_type": "Send SMS", "result": "sent"},
			{"path_id": "p1", "node_id": "u1", "step_type": "Update Field", "result": "error", "error": "bad move"}]
		self.assertEqual(self._status(log), "Failed")

	def test_a_successful_update_field_is_completed(self):
		self.assertEqual(self._status([{"path_id": "p1", "node_id": "u1", "step_type": "Update Field", "result": "updated"}]), "Completed")
