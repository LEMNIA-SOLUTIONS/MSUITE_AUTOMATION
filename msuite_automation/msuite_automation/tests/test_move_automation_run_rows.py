import unittest
from unittest.mock import MagicMock, patch

from msuite_core.patches import move_automation_run_rows as P

M = "msuite_core.patches.move_automation_run_rows"


class TestMoveRows(unittest.TestCase):
	@patch(f"{M}._columns", return_value={"name", "automation", "status"})
	@patch(f"{M}.frappe.db", new_callable=MagicMock)
	def test_moves_owned_rows_and_deletes_source(self, db, _cols):
		db.table_exists.return_value = True
		db.sql_list.return_value = ["R1", "R2"]
		self.assertEqual(P._move("Automation Run", "Messaging Automation Run", "Messaging Automation"), ["R1", "R2"])
		sqls = [c.args[0] for c in db.sql.call_args_list]
		self.assertTrue(sqls[0].startswith("insert into `tabMessaging Automation Run`"))
		self.assertTrue(sqls[1].startswith("delete from `tabAutomation Run`"))
		self.assertIn("name not in (select name from `tabMessaging Automation Run`)", db.sql_list.call_args.args[0])

	@patch(f"{M}._columns", return_value={"name", "automation"})
	@patch(f"{M}.frappe.db", new_callable=MagicMock)
	def test_noop_when_nothing_to_move(self, db, _cols):
		db.table_exists.return_value = True
		db.sql_list.return_value = []
		self.assertEqual(P._move("Automation Run", "Messaging Automation Run", "Messaging Automation"), [])
		db.sql.assert_not_called()

	@patch(f"{M}.frappe.db", new_callable=MagicMock)
	def test_noop_when_a_table_is_missing(self, db):
		db.table_exists.side_effect = lambda d: d != "Automation Flow"
		self.assertEqual(P._move("Messaging Automation Run", "Automation Run", "Automation Flow"), [])
		db.sql_list.assert_not_called()

	@patch(f"{M}._columns", return_value={"name", "status"})
	@patch(f"{M}.frappe.db", new_callable=MagicMock)
	def test_noop_without_automation_column(self, db, _cols):
		db.table_exists.return_value = True
		self.assertEqual(P._move("Automation Run", "Messaging Automation Run", "Messaging Automation"), [])

	@patch(f"{M}._move", return_value=[])
	@patch(f"{M}.frappe.db", new_callable=MagicMock)
	def test_execute_reparents_paths_both_ways_even_when_nothing_moved(self, db, _mv):
		db.table_exists.return_value = True
		P.execute()
		params = [c.args[1] for c in db.sql.call_args_list]
		self.assertEqual(params, [
			{"d": P.MSUITE, "s": P.CORE},
			{"d": P.CORE, "s": P.MSUITE},
		])
		db.commit.assert_called_once()
