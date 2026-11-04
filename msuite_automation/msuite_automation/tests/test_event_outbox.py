import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from msuite_automation.msuite_automation.constants import OUTBOX_DOCTYPE
from msuite_automation.msuite_automation.engine import executor

OUTBOX = OUTBOX_DOCTYPE
RUN_METHOD = "msuite_automation.msuite_automation.engine.executor.run_automation"
DELIVER_METHOD = "msuite_automation.msuite_automation.engine.executor.deliver_outbox"
QUEUE_SHORT = "short"
JOB_PREFIX = "outbox"
LARGE_TEXT_MULTIPLIER = 100_000
AGE_SIXTY_MINUTES = 60
AGE_FIFTY_MINUTES = 50
AGE_FORTY_MINUTES = 40
TEST_BATCH_LIMIT = 2


def outbox_rows():
	"""Fetch all outbox rows ordered by creation."""
	return frappe.get_all(OUTBOX, fields=["name", "payload", "creation"], order_by="creation asc")


class OutboxBase(unittest.TestCase):
	def setUp(self):
		"""Clear outbox rows before test execution."""
		frappe.db.delete(OUTBOX)

	def tearDown(self):
		"""Roll back pending transaction and clean up outbox table."""
		frappe.db.rollback()
		frappe.db.delete(OUTBOX)
		frappe.db.commit()

	def add_row(self, payload=None, age_minutes=0):
		"""Insert a test outbox row with optional backdated creation."""
		row = frappe.get_doc({"doctype": OUTBOX, "payload": json.dumps(payload or {"name": "A", "doctype": "ToDo", "docname": "X"})})
		row.insert(ignore_permissions=True)
		if age_minutes:
			frappe.db.set_value(OUTBOX, row.name, "creation", add_to_date(now_datetime(), minutes=-age_minutes), update_modified=False)
		return row.name


class TestOutboxDoctype(OutboxBase):
	def test_payload_round_trips_emoji_and_large_text(self):
		"""Verify payload field preserves emojis and large text payloads intact."""
		payload = json.dumps({"text": "héllo 😀 " + "x" * LARGE_TEXT_MULTIPLIER})
		name = self.add_row(json.loads(payload))
		self.assertEqual(json.loads(frappe.db.get_value(OUTBOX, name, "payload"))["text"], json.loads(payload)["text"])


class TestEnqueueRun(OutboxBase):
	def test_writes_row_and_queues_delivery_after_commit(self):
		"""Verify enqueue_run writes an outbox row and registers after-commit delivery."""
		with patch("frappe.enqueue") as enq:
			executor.enqueue_run(name="A", trigger_id="t1", doctype="ToDo", docname="X", doc_dict={"k": "v"})
			enq.assert_not_called()  # nothing is queued before the commit
			rows = outbox_rows()
			self.assertEqual(len(rows), 1)
			payload = json.loads(rows[0].payload)
			self.assertEqual((payload["name"], payload["doctype"], payload["docname"], payload["doc_dict"]), ("A", "ToDo", "X", {"k": "v"}))
			frappe.db.after_commit.run()
		enq.assert_called_once()
		self.assertEqual(enq.call_args.args[0], DELIVER_METHOD)
		self.assertEqual(enq.call_args.kwargs["outbox"], rows[0].name)
		self.assertEqual(enq.call_args.kwargs["job_id"], f"{JOB_PREFIX}:{rows[0].name}")
		self.assertTrue(enq.call_args.kwargs["deduplicate"])

	def test_rollback_leaves_no_row_and_no_job(self):
		"""Verify rollback clears outbox row and prevents delivery enqueue."""
		with patch("frappe.enqueue") as enq:
			executor.enqueue_run(name="A", trigger_id="t1", doctype="ToDo", docname="X")
			frappe.db.rollback()
			frappe.db.after_commit.run()
		enq.assert_not_called()
		self.assertEqual(outbox_rows(), [])


class TestDeliverOutbox(OutboxBase):
	def test_claims_row_and_runs_automation_once(self):
		"""Verify deliver_outbox deletes outbox row atomically and executes run_automation once."""
		name = self.add_row({"name": "A", "trigger_id": "t1", "doctype": "ToDo", "docname": "X", "value": "v"})
		with patch.object(executor, "run_automation") as run:
			executor.deliver_outbox(name)
			executor.deliver_outbox(name)
		run.assert_called_once_with(name="A", trigger_id="t1", doctype="ToDo", docname="X", value="v")
		self.assertEqual(outbox_rows(), [])

	def test_missing_row_does_nothing(self):
		"""Verify deliver_outbox exits cleanly when given a non-existent outbox row."""
		with patch.object(executor, "run_automation") as run:
			executor.deliver_outbox("does-not-exist")
		run.assert_not_called()


class TestEnqueueSafely(unittest.TestCase):
	def tearDown(self):
		"""Roll back pending database changes after test."""
		frappe.db.rollback()

	def test_redis_error_does_not_abort_later_callbacks(self):
		"""Verify enqueue exception in after-commit callback does not abort later callbacks."""
		later = []
		with patch("frappe.enqueue", side_effect=ConnectionError("redis down")), patch("frappe.log_error") as log:
			executor.enqueue_safely("m.first", queue=QUEUE_SHORT)
			frappe.db.after_commit.add(lambda: later.append(1))
			frappe.db.after_commit.run()
		self.assertEqual(later, [1])
		log.assert_called_once()

	def test_enqueues_after_commit_with_given_arguments(self):
		"""Verify enqueue_safely delegates to frappe.enqueue with exact parameters upon commit."""
		with patch("frappe.enqueue") as enq:
			executor.enqueue_safely("m.first", queue=QUEUE_SHORT, a=1)
			enq.assert_not_called()
			frappe.db.after_commit.run()
		enq.assert_called_once_with("m.first", queue=QUEUE_SHORT, a=1)

	def test_enqueue_batch_redis_error_does_not_abort_commit(self):
		"""Verify Redis failure in enqueue_batch does not abort commit callbacks."""
		with patch("frappe.enqueue", side_effect=ConnectionError("redis down")), patch("frappe.log_error") as log:
			executor.enqueue_batch("AUTO-1", "t1", "Sales Order", ["SO-1"])
			frappe.db.after_commit.run()
		log.assert_called_once()


class TestRedeliverOutbox(OutboxBase):
	def test_only_rows_older_than_the_age_are_requeued(self):
		"""Verify only rows older than the redelivery threshold are enqueued."""
		old = self.add_row(age_minutes=executor.OUTBOX_REDELIVER_AFTER_MINUTES + 1)
		self.add_row(age_minutes=0)
		with patch("frappe.enqueue") as enq:
			executor.redeliver_outbox()
		enq.assert_called_once()
		self.assertEqual(enq.call_args.kwargs["outbox"], old)
		self.assertEqual(enq.call_args.kwargs["job_id"], f"{JOB_PREFIX}:{old}")

	def test_batch_is_capped_oldest_first_with_warning(self):
		"""Verify redeliver_outbox caps batch size, picks oldest first, and logs warning."""
		with patch.object(executor, "OUTBOX_REDELIVER_BATCH", TEST_BATCH_LIMIT):
			oldest = self.add_row(age_minutes=AGE_SIXTY_MINUTES)
			middle = self.add_row(age_minutes=AGE_FIFTY_MINUTES)
			self.add_row(age_minutes=AGE_FORTY_MINUTES)
			with patch("frappe.enqueue") as enq, patch("frappe.logger") as logger:
				executor.redeliver_outbox()
		self.assertEqual([c.kwargs["outbox"] for c in enq.call_args_list], [oldest, middle])
		logger.return_value.warning.assert_called_once()

	def test_one_failing_enqueue_does_not_stop_the_rest(self):
		"""Verify redelivery loop continues even if enqueue fails for one outbox item."""
		first = self.add_row(age_minutes=AGE_SIXTY_MINUTES)
		second = self.add_row(age_minutes=AGE_FIFTY_MINUTES)
		calls = []

		def flaky(method, **kw):
			"""Simulate enqueue failure on the first item."""
			calls.append(kw["outbox"])
			if kw["outbox"] == first:
				raise ConnectionError("redis down")

		with patch("frappe.enqueue", side_effect=flaky), patch("frappe.log_error"):
			executor.redeliver_outbox()
		self.assertEqual(calls, [first, second])

	def test_lost_job_is_delivered_end_to_end(self):
		"""Verify lost background job is redelivered and successfully executed end-to-end."""
		with patch("frappe.enqueue"):
			executor.enqueue_run(name="A", trigger_id="t1", doctype="ToDo", docname="X")
			frappe.db.commit()  # the queued job is "lost": the mock never runs it
		name = outbox_rows()[0].name
		frappe.db.set_value(OUTBOX, name, "creation", add_to_date(now_datetime(), minutes=-AGE_SIXTY_MINUTES), update_modified=False)
		queued = []
		with patch("frappe.enqueue", side_effect=lambda m, **kw: queued.append(kw["outbox"])):
			executor.redeliver_outbox()
		with patch.object(executor, "run_automation") as run:
			for outbox in queued:
				executor.deliver_outbox(outbox)
		self.assertEqual(queued, [name])
		run.assert_called_once()
