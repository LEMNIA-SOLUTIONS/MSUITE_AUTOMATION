import json
import re
import unittest
from pathlib import Path

try:
	from msuite_automation.msuite_automation import constants
except Exception:
	import importlib.util

	constants_path = Path(__file__).resolve().parent.parent / "constants.py"
	spec = importlib.util.spec_from_file_location("constants", str(constants_path))
	constants = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(constants)


class TestRunDoctypeRename(unittest.TestCase):
	def test_constants_run_doctype(self):
		self.assertEqual(constants.RUN_DOCTYPE, "Messaging Automation Run")

	def test_doctype_json_name(self):
		base_dir = Path(__file__).resolve().parent.parent
		json_path = base_dir / "doctype" / "messaging_automation_run" / "messaging_automation_run.json"
		self.assertTrue(json_path.exists(), f"JSON file missing: {json_path}")
		with open(json_path, "r", encoding="utf-8") as f:
			data = json.load(f)
		self.assertEqual(data.get("name"), "Messaging Automation Run")

	def test_old_folder_does_not_exist(self):
		base_dir = Path(__file__).resolve().parent.parent
		old_folder = base_dir / "doctype" / "automation_run"
		self.assertFalse(old_folder.exists(), f"Old folder still exists: {old_folder}")

	def test_no_py_file_references_old_table(self):
		base_dir = Path(__file__).resolve().parent.parent
		pattern = re.compile(r"tabAutomation Run`(\s|`|$)")
		violating_files = []
		for py_file in base_dir.rglob("*.py"):
			rel_parts = py_file.relative_to(base_dir).parts
			# exclude tests and patch files
			if "tests" in rel_parts or "patches" in rel_parts:
				continue
			content = py_file.read_text(encoding="utf-8")
			if pattern.search(content):
				violating_files.append(str(py_file.relative_to(base_dir)))
		self.assertEqual(violating_files, [], f"Old table references found in: {violating_files}")


if __name__ == "__main__":
	unittest.main()
