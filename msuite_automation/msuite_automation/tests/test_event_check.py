import json
import unittest

import frappe
from frappe.utils import add_to_date, now_datetime

from msuite_automation.msuite_automation.engine.event_check import event_met


class TestEventCheck(unittest.TestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		frappe.db.delete("ToDo", {"description": ("like", "ZZ event check%")})
		frappe.db.delete("Note", {"title": ("like", "ZZ event check%")})
		frappe.db.delete("Channel Message", {"sender_phone": ("like", "9199999000%")})
		frappe.db.delete("Channel Message", {"text_content": "ZZ reply"})

	def test_same_doc_mode_true(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "ZZ event check same doc", "status": "Open"}).insert(ignore_permissions=True)
		step = frappe._dict(
			step_type="Wait for Event",
			check_doctype="",
			condition_rules=json.dumps([{"field": "status", "operator": "=", "value": "Open"}]),
			condition_logic="All",
		)
		self.assertTrue(event_met(step, todo, now_datetime()))

	def test_same_doc_mode_false(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "ZZ event check same doc", "status": "Open"}).insert(ignore_permissions=True)
		step = frappe._dict(
			step_type="Wait for Event",
			check_doctype="",
			condition_rules=json.dumps([{"field": "status", "operator": "=", "value": "Closed"}]),
			condition_logic="All",
		)
		self.assertFalse(event_met(step, todo, now_datetime()))

	def test_same_doc_mode_fresh_read(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "ZZ event check fresh read", "status": "Open"}).insert(ignore_permissions=True)
		# Update database directly
		frappe.db.set_value("ToDo", todo.name, "status", "Closed")
		# todo passed has stale status "Open"
		step = frappe._dict(
			step_type="Wait for Event",
			check_doctype="",
			condition_rules=json.dumps([{"field": "status", "operator": "=", "value": "Closed"}]),
			condition_logic="All",
		)
		self.assertTrue(event_met(step, todo, now_datetime()))

	def test_linked_mode_older_record_ignored(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "ZZ event check linked older", "status": "Open"}).insert(ignore_permissions=True)
		# Note created before run_started_at
		frappe.get_doc({"doctype": "Note", "title": f"ZZ event check {todo.name}", "content": "hello"}).insert(ignore_permissions=True)
		# run started after the Note was created
		run_started_at = add_to_date(now_datetime(), minutes=5)
		step = frappe._dict(
			step_type="Wait for Event",
			check_doctype="Note",
			check_link_field="title",
			check_match_field="name",
		)
		# The link value is "ZZ event check " + todo.name, but let's test matching match_value directly
		# Let's match on description
		step.check_match_field = "name"
		# When title equals todo.name, but created before run_started_at:
		frappe.get_doc({"doctype": "Note", "title": todo.name, "content": "hello"}).insert(ignore_permissions=True)
		self.assertFalse(event_met(step, todo, run_started_at))

	def test_linked_mode_trigger_doc_itself_ignored(self):
		run_started = add_to_date(now_datetime(), minutes=-5)
		todo = frappe.get_doc({"doctype": "ToDo", "description": "ZZ event check trigger self", "status": "Open"}).insert(ignore_permissions=True)
		# Check ToDo where description == todo.description (same doc)
		step = frappe._dict(
			step_type="Wait for Event",
			check_doctype="ToDo",
			check_link_field="description",
			check_match_field="description",
		)
		# No other ToDo exists with this description, so it should exclude itself and return False
		self.assertFalse(event_met(step, todo, run_started))

	def test_linked_mode_matching_newer_record(self):
		run_started = now_datetime()
		todo = frappe.get_doc({"doctype": "ToDo", "description": "ZZ event check newer record", "status": "Open"}).insert(ignore_permissions=True)
		note = frappe.get_doc({"doctype": "Note", "title": f"ZZ event check note {todo.name}", "content": "Approved"}).insert(ignore_permissions=True)
		step = frappe._dict(
			step_type="Wait for Event",
			check_doctype="Note",
			check_link_field="name",
			check_match_field="name",
		)
		# Note where title == note.title
		step_matching = frappe._dict(
			step_type="Wait for Event",
			check_doctype="Note",
			check_link_field="title",
			check_match_field="name",
			condition_rules=json.dumps([{"field": "content", "operator": "=", "value": "Approved"}]),
			condition_logic="All",
		)
		# note.title is set to todo.name
		note.title = todo.name
		note.save(ignore_permissions=True)
		self.assertTrue(event_met(step_matching, todo, run_started))

	def test_linked_mode_rules_failing(self):
		run_started = now_datetime()
		todo = frappe.get_doc({"doctype": "ToDo", "description": "ZZ event check rules failing", "status": "Open"}).insert(ignore_permissions=True)
		note = frappe.get_doc({"doctype": "Note", "title": todo.name, "content": "Rejected"}).insert(ignore_permissions=True)
		step = frappe._dict(
			step_type="Wait for Event",
			check_doctype="Note",
			check_link_field="title",
			check_match_field="name",
			condition_rules=json.dumps([{"field": "content", "operator": "=", "value": "Approved"}]),
			condition_logic="All",
		)
		self.assertFalse(event_met(step, todo, run_started))

	def test_replied_case_channel_message(self):
		"""Assert only inbound messages created AFTER run started count; outbound/older do not."""
		phone = "91999990001"
		run_started = now_datetime()

		trigger_doc = frappe._dict(
			doctype="Sales Order",
			name="SO-ZZ-REPLY-001",
			contact_phone=phone,
		)

		step = frappe._dict(
			step_type="Wait for Event",
			check_doctype="Channel Message",
			check_link_field="sender_phone",
			check_match_field="contact_phone",
			condition_rules=json.dumps([
				{"field": "direction", "operator": "=", "value": "Inbound"},
				{"field": "channel_type", "operator": "=", "value": "WhatsApp"},
			]),
			condition_logic="All",
		)

		# 1. No messages exist initially
		self.assertFalse(event_met(step, trigger_doc, run_started))

		# 2. Older inbound message created BEFORE run_started must NOT count
		older_msg = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": "WhatsApp",
			"direction": "Inbound",
			"sender_phone": phone,
			"text_content": "Earlier message before run started",
		})
		older_msg.flags.suppress_side_effects = True
		older_msg.insert(ignore_permissions=True, ignore_mandatory=True)
		frappe.db.set_value("Channel Message", older_msg.name, "creation", add_to_date(run_started, minutes=-10), update_modified=False)
		self.assertFalse(event_met(step, trigger_doc, run_started))

		# 3. Outbound message created AFTER run_started must NOT count
		outbound_msg = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": "WhatsApp",
			"direction": "Outbound",
			"sender_phone": phone,
			"text_content": "Our outbound follow up",
		})
		outbound_msg.flags.suppress_side_effects = True
		outbound_msg.insert(ignore_permissions=True, ignore_mandatory=True)
		frappe.db.set_value("Channel Message", outbound_msg.name, "creation", add_to_date(run_started, seconds=10), update_modified=False)
		self.assertFalse(event_met(step, trigger_doc, run_started))

		# 4. Inbound message on different channel (Instagram DM) must NOT count
		other_channel_msg = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": "Instagram DM",
			"direction": "Inbound",
			"sender_phone": phone,
			"text_content": "Inbound from Instagram",
		})
		other_channel_msg.flags.suppress_side_effects = True
		other_channel_msg.insert(ignore_permissions=True, ignore_mandatory=True)
		frappe.db.set_value("Channel Message", other_channel_msg.name, "creation", add_to_date(run_started, seconds=20), update_modified=False)
		self.assertFalse(event_met(step, trigger_doc, run_started))

		# 5. Inbound WhatsApp message from a different phone must NOT count
		different_phone_msg = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": "WhatsApp",
			"direction": "Inbound",
			"sender_phone": "91999990002",
			"text_content": "Inbound from another customer",
		})
		different_phone_msg.flags.suppress_side_effects = True
		different_phone_msg.insert(ignore_permissions=True, ignore_mandatory=True)
		frappe.db.set_value("Channel Message", different_phone_msg.name, "creation", add_to_date(run_started, seconds=30), update_modified=False)
		self.assertFalse(event_met(step, trigger_doc, run_started))

		# 6. Valid inbound WhatsApp message from the order's phone created AFTER run_started must count
		reply_msg = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": "WhatsApp",
			"direction": "Inbound",
			"sender_phone": phone,
			"text_content": "Thanks, received the order!",
		})
		reply_msg.flags.suppress_side_effects = True
		reply_msg.insert(ignore_permissions=True, ignore_mandatory=True)
		frappe.db.set_value("Channel Message", reply_msg.name, "creation", add_to_date(run_started, seconds=40), update_modified=False)
		self.assertTrue(event_met(step, trigger_doc, run_started))

	def _inbound(self, sender_phone, run_started):
		msg = frappe.get_doc({"doctype": "Channel Message", "channel_type": "WhatsApp", "direction": "Inbound",
			"sender_phone": sender_phone, "text_content": "ZZ reply"})
		msg.flags.suppress_side_effects = True
		msg.insert(ignore_permissions=True, ignore_mandatory=True)
		frappe.db.set_value("Channel Message", msg.name, "creation", add_to_date(run_started, seconds=10), update_modified=False)

	def _reply_step(self):
		return frappe._dict(step_type="Wait for Event", check_doctype="Channel Message", check_link_field="sender_phone",
			check_match_field="contact_phone", condition_rules="[]", condition_logic="All")

	def test_order_phone_formats_match_raw_digit_sender(self):
		run_started = now_datetime()
		self._inbound("919999900021", run_started)
		for order_phone in ("+91 99999 00021", "+919999900021", "9999900021", "919999900021"):
			doc = frappe._dict(doctype="Sales Order", name="SO-ZZ-FMT", contact_phone=order_phone)
			self.assertTrue(event_met(self._reply_step(), doc, run_started), order_phone)

	def test_non_phone_match_value_stays_exact(self):
		from msuite_automation.msuite_automation.engine.event_check import _match_values
		self.assertEqual(_match_values("CUST-0001"), ["CUST-0001"])
		self.assertEqual(_match_values("CUST-9876543210"), ["CUST-9876543210"])
		self.assertEqual(_match_values("919876543210", phone_field=False), ["919876543210"])
		self.assertIn("919999900021", _match_values("+91 99999 00021"))
		self.assertEqual(_match_values("   "), [])

	def test_blank_match_value_never_matches_empty_records(self):
		run_started = now_datetime()
		self._inbound("", run_started)
		doc = frappe._dict(doctype="Sales Order", name="SO-ZZ-BLANK", contact_phone="   ")
		self.assertFalse(event_met(self._reply_step(), doc, run_started))

	def test_phone_formats_match_with_rules(self):
		run_started = now_datetime()
		self._inbound("919999900022", run_started)
		step = self._reply_step()
		step.condition_rules = json.dumps([{"field": "direction", "operator": "=", "value": "Inbound"}])
		doc = frappe._dict(doctype="Sales Order", name="SO-ZZ-RULES", contact_phone="+91 99999 00022")
		self.assertTrue(event_met(step, doc, run_started))
