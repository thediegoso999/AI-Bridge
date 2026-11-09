import tempfile
import unittest
from pathlib import Path

from aibridge import inspect_repository


class InspectRepositoryTests(unittest.TestCase):
    def test_reports_repository_files_in_stable_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "README.md").write_text("Project\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "zeta.yaml").touch()
            (workflows / "alpha.yml").touch()
            (root / ".devcontainer").mkdir()
            (root / ".devcontainer" / "devcontainer.json").touch()
            tests = root / "tests" / "unit"
            tests.mkdir(parents=True)
            (tests / "test_example.py").touch()

            snapshot = inspect_repository(root)

        self.assertEqual(snapshot.name, root.name)
        self.assertTrue(snapshot.has_readme)
        self.assertTrue(snapshot.has_devcontainer)
        self.assertTrue(snapshot.has_python_project)
        self.assertEqual(
            snapshot.workflow_files,
            (".github/workflows/alpha.yml", ".github/workflows/zeta.yaml"),
        )
        self.assertEqual(snapshot.test_files, ("tests/unit/test_example.py",))

    def test_reports_missing_optional_project_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot = inspect_repository(temporary_directory)

        self.assertFalse(snapshot.has_readme)
        self.assertFalse(snapshot.has_devcontainer)
        self.assertFalse(snapshot.has_python_project)
        self.assertEqual(snapshot.workflow_files, ())
        self.assertEqual(snapshot.test_files, ())

    def test_rejects_a_path_that_is_not_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            file_path = Path(temporary_directory) / "file.txt"
            file_path.touch()

            with self.assertRaisesRegex(ValueError, "Not a directory"):
                inspect_repository(file_path)


if __name__ == "__main__":
    unittest.main()