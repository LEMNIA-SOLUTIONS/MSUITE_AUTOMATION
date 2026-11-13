"""AUT-103: the Update Field step saves through doc.save(), so the record's own rules apply, and its save never
starts other automations. Runs against the local test DB; no live calls."""

from unittest.mock import patch

import frappe
from frappe.desk.doctype.todo.todo import ToDo
from frappe.tests import IntegrationTestCase

from msuite_automation.msuite_automation.constants import AUTOMATION_WRITE_FLAG, SYSTEM_USER
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


OWNER = "perm-system-manager@example.com"  # real enabled user in the dev DB
REJECT = frappe.ValidationError("Transition 'New' → 'Follow-up Required' is not a valid Lead pipeline move.")


class TestUpdateFieldRunsAsAutomationOwner(IntegrationTestCase):
	"""AUT-103 D3: a scheduler run (session Administrator) saves as the automation owner, so rules that skip
	Administrator (the Lead pipeline) still apply."""

	def setUp(self):
		self.todo = frappe.get_doc({"doctype": "ToDo", "description": "ZZ aut103 owner", "status": "Open"}).insert(ignore_permissions=True)
		self.prev_user = frappe.session.user
		frappe.set_user(SYSTEM_USER)

	def tearDown(self):
		frappe.set_user(self.prev_user)
		frappe.db.rollback()

	def _ctx(self, owner):
		return frappe._dict(automation=frappe._dict(name="MA-X", send_frequency="Every time", owner=owner), doc=self.todo)

	def _run(self, owner, side_effect=None):
		seen = []

		original = ToDo.validate

		def validate(doc, *_a, **_k):
			seen.append(frappe.session.user)
			if side_effect:
				raise side_effect
			original(doc)  # keep ToDo's own validate (sets _assignment)

		with patch.object(ToDo, "validate", validate):
			try:
				return seen, steps.execute(self._ctx(owner), _step())
			finally:
				self.seen = seen

	def test_saves_as_owner_and_restores_the_session_user(self):
		seen, result = self._run(OWNER)
		self.assertEqual(result, "updated")
		self.assertEqual(seen, [OWNER])
		self.assertEqual(frappe.session.user, SYSTEM_USER)

	def test_a_rejected_move_fails_the_step_and_restores_the_session_user(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._run(OWNER, side_effect=REJECT)
		self.assertIn("not a valid Lead pipeline move", str(cm.exception))
		self.assertEqual(self.seen, [OWNER])
		self.assertEqual(frappe.session.user, SYSTEM_USER)
		self.assertEqual(frappe.db.get_value("ToDo", self.todo.name, "status"), "Open")

	def test_administrator_owned_automation_keeps_the_bypass(self):
		with patch.object(frappe, "set_user") as set_user:
			seen, result = self._run(SYSTEM_USER)
		set_user.assert_not_called()
		self.assertEqual((seen, result), ([SYSTEM_USER], "updated"))

	def test_missing_owner_does_not_impersonate(self):
		with patch.object(frappe, "set_user") as set_user:
			self._run(None)
		set_user.assert_not_called()

	def test_normal_session_user_is_not_impersonated(self):
		frappe.set_user(OWNER)
		with patch.object(frappe, "set_user") as set_user:
			seen, _ = self._run("someone-else@example.com")
		set_user.assert_not_called()
		self.assertEqual(seen, [OWNER])

	def test_unusable_owner_fails_the_step_without_falling_back(self):
		for owner, enabled in (("ghost-aut103@example.com", None), (OWNER, 0)):
			with self.subTest(owner=owner), patch("frappe.db.get_value", return_value=enabled):
				with patch("frappe.desk.doctype.todo.todo.ToDo.validate") as validate:
					with self.assertRaises(frappe.ValidationError) as cm:
						steps.execute(self._ctx(owner), _step())
				validate.assert_not_called()
				self.assertIn(owner, str(cm.exception))
				self.assertEqual(frappe.session.user, SYSTEM_USER)

	def test_live_a_real_user_cannot_make_an_invalid_lead_move(self):
		"""Real CRM Lead from the dev DB (rolled back in tearDown): New -> Follow-up Required is rejected for the owner."""
		lead = frappe.db.get_value("CRM Lead", {"status": "New"}, "name")
		if not lead:
			self.skipTest("no CRM Lead in status New on this site")
		ctx = frappe._dict(automation=frappe._dict(name="MA-X", send_frequency="Every time", owner=OWNER), doc=frappe._dict(doctype="CRM Lead", name=lead))
		with self.assertRaises(frappe.ValidationError) as cm:
			steps.execute(ctx, _step("status", "Follow-up Required"))
		self.assertIn("not a valid Lead pipeline move", str(cm.exception))
		self.assertEqual(frappe.session.user, SYSTEM_USER)


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
