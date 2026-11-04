import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, nowdate

from msuite_automation.msuite_automation.constants import (
	DATE_BASED, EVERY_TIME, ONCE_PER_DAY, ONCE_PER_DOCUMENT, RUN_DOCTYPE, SCHEDULE,
)
from msuite_automation.msuite_automation.engine import executor
from msuite_automation.msuite_automation.engine import triggers as T

EX = "msuite_automation.msuite_automation.engine.executor"
TR = "msuite_automation.msuite_automation.engine.triggers"


class TestScheduledBatching(unittest.TestCase):
	def test_1200_docs_enqueued_in_batches_of_at_most_25_and_no_limit_10000(self):
		all_docs = [f"SO-{i:04d}" for i in range(1, 1201)]

		def mock_get_all(dt, **kwargs):
			limit = kwargs.get("limit")
			self.assertNotEqual(limit, 10000, "Must not call get_all with limit=10000")
			if dt == RUN_DOCTYPE:
				if kwargs.get("filters", {}).get("status") == "Failed":
					return []  # the failed-run cap query: no failures in this scenario
				return []
			if dt == "Sales Order":
				filters = kwargs.get("filters", [])
				last = ""
				for f in filters:
					if isinstance(f, list) and len(f) == 3 and f[0] == "name" and f[1] == ">":
						last = f[2]
				matching = [d for d in all_docs if d > last]
				return matching[:limit] if limit else matching
			return []

		auto_doc = frappe._dict(
			name="AUTO-1200",
			disabled=0,
			send_frequency=ONCE_PER_DOCUMENT,
			triggers=[frappe._dict(trigger_id="t1", trigger_type=SCHEDULE, reference_doctype="Sales Order", condition=None)],
		)

		with patch("frappe.get_doc", return_value=auto_doc), \
			patch("frappe.get_all", side_effect=mock_get_all) as get_all_mock, \
			patch("frappe.enqueue") as enqueue_mock:

			executor.run_scheduled("AUTO-1200", trigger_id="t1")
			frappe.db.after_commit.run()

		self.assertTrue(enqueue_mock.called, "frappe.enqueue should have been called")
		enqueued_docs = []
		for c in enqueue_mock.call_args_list:
			self.assertEqual(c.kwargs.get("queue"), "short")
			self.assertEqual(c.kwargs.get("timeout"), 1500)
			self.assertEqual(c.kwargs.get("name"), "AUTO-1200")
			self.assertEqual(c.kwargs.get("trigger_id"), "t1")
			self.assertEqual(c.kwargs.get("doctype"), "Sales Order")
			batch = c.kwargs.get("docnames")
			self.assertIsNotNone(batch)
			self.assertLessEqual(len(batch), 25, f"Batch size {len(batch)} exceeds 25")
			enqueued_docs.extend(batch)

		self.assertEqual(enqueued_docs, all_docs)
		self.assertEqual(len(enqueue_mock.call_args_list), 48)  # 1200 / 25 = 48 batches

	def test_docs_with_existing_runs_skipped_for_once_per_doc_and_every_time_enqueues_all(self):
		names = ["SO-1", "SO-2", "SO-3", "SO-4", "SO-5"]

		# Test Once per document
		def mock_get_all_once(dt, **kwargs):
			if dt == "Sales Order":
				last = ""
				for f in kwargs.get("filters", []):
					if isinstance(f, list) and f[0] == "name" and f[1] == ">":
						last = f[2]
				return [n for n in names if n > last]
			if dt == RUN_DOCTYPE:
				if kwargs.get("filters", {}).get("status") == "Failed":
					return []  # the failed-run cap query: no failures in this scenario
				return ["SO-2", "SO-4"]
			return []

		with patch("frappe.get_all", side_effect=mock_get_all_once):
			pages = list(executor.iter_pending_names("AUTO-ONCE", ONCE_PER_DOCUMENT, "Sales Order"))

		self.assertEqual(pages, [["SO-1", "SO-3", "SO-5"]])

		# Test Every time: does not query RUN_DOCTYPE and yields all
		def mock_get_all_every(dt, **kwargs):
			if dt == "Sales Order":
				last = ""
				for f in kwargs.get("filters", []):
					if isinstance(f, list) and f[0] == "name" and f[1] == ">":
						last = f[2]
				return [n for n in names if n > last]
			if dt == RUN_DOCTYPE:
				self.fail("Every time should never query RUN_DOCTYPE")
			return []

		with patch("frappe.get_all", side_effect=mock_get_all_every):
			pages = list(executor.iter_pending_names("AUTO-EVERY", EVERY_TIME, "Sales Order"))

		self.assertEqual(pages, [["SO-1", "SO-2", "SO-3", "SO-4", "SO-5"]])

	def test_once_per_day_only_skips_runs_from_today(self):
		names = ["SO-1", "SO-2", "SO-3"]

		def mock_get_all(dt, **kwargs):
			if dt == "Sales Order":
				last = ""
				for f in kwargs.get("filters", []):
					if isinstance(f, list) and f[0] == "name" and f[1] == ">":
						last = f[2]
				return [n for n in names if n > last]
			if dt == RUN_DOCTYPE:
				if kwargs.get("filters", {}).get("status") == "Failed":
					return []  # the failed-run cap query: no failures in this scenario
				filters = kwargs.get("filters", {})
				self.assertIn("creation", filters)
				self.assertEqual(filters["creation"], (">=", nowdate()))
				# Simulate SO-1 ran today, but SO-2 ran yesterday (not returned because of creation filter)
				return ["SO-1"]
			return []

		with patch("frappe.get_all", side_effect=mock_get_all):
			pages = list(executor.iter_pending_names("AUTO-DAY", ONCE_PER_DAY, "Sales Order"))

		self.assertEqual(pages, [["SO-2", "SO-3"]])

	def test_zero_pending_no_jobs_and_no_error(self):
		auto_doc = frappe._dict(
			name="AUTO-EMPTY",
			disabled=0,
			send_frequency=ONCE_PER_DOCUMENT,
			triggers=[frappe._dict(trigger_id="t1", trigger_type=SCHEDULE, reference_doctype="Sales Order", condition=None)],
		)

		with patch("frappe.get_doc", return_value=auto_doc), \
			patch("frappe.get_all", return_value=[]), \
			patch("frappe.enqueue") as enqueue_mock, \
			patch("frappe.log_error") as log_mock:

			executor.run_scheduled("AUTO-EMPTY", trigger_id="t1")

		enqueue_mock.assert_not_called()
		log_mock.assert_not_called()

	def test_ceiling_reached_logs_error_naming_automation(self):
		# Patch SCHEDULE_CEILING to small value 5
		all_docs = [f"SO-{i:03d}" for i in range(1, 21)]
		already_run = set(f"SO-{i:03d}" for i in range(1, 11))  # SO-001 .. SO-010 already ran

		def mock_get_all(dt, **kwargs):
			if dt == RUN_DOCTYPE:
				if kwargs.get("filters", {}).get("status") == "Failed":
					return []  # the failed-run cap query: no failures in this scenario
				ref_names = kwargs.get("filters", {}).get("reference_name", (None, []))[1]
				return [d for d in ref_names if d in already_run]
			if dt == "Sales Order":
				last = ""
				for f in kwargs.get("filters", []):
					if isinstance(f, list) and f[0] == "name" and f[1] == ">":
						last = f[2]
				matching = [d for d in all_docs if d > last]
				return matching[: kwargs.get("limit", 2)]
			return []

		with patch(f"{EX}.SCHEDULE_CEILING", 5), \
			patch(f"{EX}.SCHEDULE_BATCH", 2), \
			patch("frappe.get_all", side_effect=mock_get_all), \
			patch("frappe.log_error") as log_mock:

			pages = list(executor.iter_pending_names("AUTO-CEIL", ONCE_PER_DOCUMENT, "Sales Order"))

		# SO-001..SO-010 scanned and skipped; docs beyond prefix are reached and count toward ceiling
		yielded_docs = [d for p in pages for d in p]
		self.assertEqual(yielded_docs, ["SO-011", "SO-012", "SO-013", "SO-014", "SO-015", "SO-016"])
		self.assertEqual(len(yielded_docs), 6)  # 2 + 2 + 2 = 6 pending >= 5
		log_mock.assert_called_once()
		self.assertIn("AUTO-CEIL", log_mock.call_args.kwargs.get("title", ""))
		self.assertIn("scheduled run hit", log_mock.call_args.kwargs.get("title", ""))

	def test_all_docs_already_run_yields_nothing_and_no_error_logged(self):
		all_docs = [f"SO-{i:03d}" for i in range(1, 15)]

		def mock_get_all(dt, **kwargs):
			if dt == RUN_DOCTYPE:
				if kwargs.get("filters", {}).get("status") == "Failed":
					return []  # the failed-run cap query: no failures in this scenario
				return kwargs.get("filters", {}).get("reference_name", (None, []))[1]
			if dt == "Sales Order":
				last = ""
				for f in kwargs.get("filters", []):
					if isinstance(f, list) and f[0] == "name" and f[1] == ">":
						last = f[2]
				matching = [d for d in all_docs if d > last]
				return matching[: kwargs.get("limit", 2)]
			return []

		with patch(f"{EX}.SCHEDULE_CEILING", 5), \
			patch(f"{EX}.SCHEDULE_BATCH", 2), \
			patch("frappe.get_all", side_effect=mock_get_all), \
			patch("frappe.log_error") as log_mock:

			pages = list(executor.iter_pending_names("AUTO-SKIP-ALL", ONCE_PER_DOCUMENT, "Sales Order"))

		self.assertEqual(pages, [])
		log_mock.assert_not_called()

	def test_scan_limit_reached_stops_and_logs_scan_limit_error(self):
		all_docs = [f"SO-{i:03d}" for i in range(1, 25)]

		def mock_get_all(dt, **kwargs):
			if dt == RUN_DOCTYPE:
				if kwargs.get("filters", {}).get("status") == "Failed":
					return []  # the failed-run cap query: no failures in this scenario
				return kwargs.get("filters", {}).get("reference_name", (None, []))[1]
			if dt == "Sales Order":
				last = ""
				for f in kwargs.get("filters", []):
					if isinstance(f, list) and f[0] == "name" and f[1] == ">":
						last = f[2]
				matching = [d for d in all_docs if d > last]
				return matching[: kwargs.get("limit", 2)]
			return []

		with patch(f"{EX}.SCHEDULE_SCAN_LIMIT", 6), \
			patch(f"{EX}.SCHEDULE_BATCH", 2), \
			patch("frappe.get_all", side_effect=mock_get_all), \
			patch("frappe.log_error") as log_mock:

			pages = list(executor.iter_pending_names("AUTO-SCAN-LIMIT", ONCE_PER_DOCUMENT, "Sales Order"))

		self.assertEqual(pages, [])
		log_mock.assert_called_once()
		self.assertIn("AUTO-SCAN-LIMIT", log_mock.call_args.kwargs.get("title", ""))
		self.assertIn("scan limit", log_mock.call_args.kwargs.get("title", ""))

	def test_autoincrement_integer_names_normalized_and_yield_original_values(self):
		int_names = [101, 102, 103, 104]

		def mock_get_all(dt, **kwargs):
			if dt == RUN_DOCTYPE:
				if kwargs.get("filters", {}).get("status") == "Failed":
					return []  # the failed-run cap query: no failures in this scenario
				return ["102", "104"]
			if dt == "Autoincrement Doctype":
				last = kwargs.get("filters", [["name", ">", ""]])[0][2]
				return [n for n in int_names if n > (last or 0)]
			return []

		with patch("frappe.get_all", side_effect=mock_get_all):
			pages = list(executor.iter_pending_names("AUTO-INT", ONCE_PER_DOCUMENT, "Autoincrement Doctype"))

		self.assertEqual(pages, [[101, 103]])
		self.assertIsInstance(pages[0][0], int)
		self.assertIsInstance(pages[0][1], int)

	def test_run_batch_commits_after_each_doc_and_survives_raising(self):
		calls = []

		def mock_run_automation(**kwargs):
			docname = kwargs.get("docname")
			calls.append(docname)
			if docname == "SO-FAIL":
				raise RuntimeError("Something exploded")
			return f"run-{docname}"

		with patch(f"{EX}.run_automation", side_effect=mock_run_automation), \
			patch("frappe.db.commit") as commit_mock, \
			patch("frappe.db.rollback") as rollback_mock, \
			patch("frappe.log_error") as log_mock:

			executor.run_batch("AUTO-BATCH", "t1", "Sales Order", ["SO-1", "SO-FAIL", "SO-3"])

		self.assertEqual(calls, ["SO-1", "SO-FAIL", "SO-3"])
		self.assertEqual(commit_mock.call_count, 3)
		rollback_mock.assert_called_once()
		log_mock.assert_called_once()

	def test_date_based_uses_iter_pending_names_with_date_filters(self):
		day = add_to_date(nowdate(), days=2)
		trigger_row = frappe._dict(
			parent="AUTO-DATE",
			trigger_id="t_date",
			reference_doctype="Delivery Note",
			date_field="posting_date",
			offset_days=2,
			offset_direction="Before",
			send_frequency=ONCE_PER_DOCUMENT,
		)

		pending_docs = [f"DN-{i}" for i in range(1, 31)]  # 30 docs

		with patch.object(T, "_trigger_rows", return_value=[trigger_row]), \
			patch(f"{EX}.iter_pending_names", return_value=[pending_docs]) as iter_mock, \
			patch(f"{EX}.enqueue_batch") as enqueue_mock:

			T.run_date_based()

		iter_mock.assert_called_once_with(
			"AUTO-DATE",
			ONCE_PER_DOCUMENT,
			"Delivery Note",
			base_filters=[
				["posting_date", ">=", f"{day} 00:00:00"],
				["posting_date", "<=", f"{day} 23:59:59.999999"],
			],
		)
		self.assertEqual(enqueue_mock.call_count, 2)
		first_batch = enqueue_mock.call_args_list[0].args[3]
		second_batch = enqueue_mock.call_args_list[1].args[3]
		self.assertEqual(len(first_batch), 25)
		self.assertEqual(len(second_batch), 5)
		self.assertEqual(first_batch + second_batch, pending_docs)

	def test_trigger_rows_selects_send_frequency(self):
		with patch("frappe.db.sql") as sql_mock:
			T._trigger_rows(DATE_BASED)

		self.assertTrue(sql_mock.called)
		query = sql_mock.call_args[0][0]
		self.assertIn("a.send_frequency", query)

	def test_enqueue_batch_passes_job_timeout_1500(self):
		with patch("frappe.enqueue") as enqueue_mock:
			executor.enqueue_batch("AUTO-1", "t1", "Sales Order", ["SO-1", "SO-2"])
			enqueue_mock.assert_not_called()  # queued only after the commit
			frappe.db.after_commit.run()

		enqueue_mock.assert_called_once_with(
			"msuite_automation.msuite_automation.engine.executor.run_batch",
			queue="short",
			timeout=1500,
			name="AUTO-1",
			trigger_id="t1",
			doctype="Sales Order",
			docnames=["SO-1", "SO-2"],
		)
