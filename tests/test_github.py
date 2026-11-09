import json
import subprocess
import unittest
from unittest.mock import patch

from aibridge.github import GitHubClient, GitHubUnavailable


class GitHubClientTests(unittest.TestCase):
    @patch("aibridge.github.subprocess.run")
    def test_lists_repositories_using_github_cli_auth(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["gh"],
            0,
            json.dumps(
                [
                    {
                        "full_name": "octo/project",
                        "private": True,
                        "default_branch": "main",
                        "updated_at": "2026-11-09T10:00:00Z",
                        "token": "must-not-be-returned",
                    }
                ]
            ),
            "",
        )

        repositories = GitHubClient().repositories()

        self.assertEqual(repositories[0]["full_name"], "octo/project")
        self.assertTrue(repositories[0]["private"])
        self.assertNotIn("token", repositories[0])
        self.assertEqual(run.call_args.args[0][0:2], ["gh", "api"])
        self.assertNotIn("token", run.call_args.args[0])

    @patch("aibridge.github.subprocess.run")
    def test_reports_missing_auth_without_echoing_command_output(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["gh"], 1, "", "gh: token=private-value not logged in"
        )

        status = GitHubClient().connection_status()

        self.assertFalse(status["connected"])
        self.assertNotIn("private-value", status["message"])

    @patch("aibridge.github.subprocess.run")
    def test_queries_workflow_runs_read_only_and_returns_summary_fields(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["gh"],
            0,
            json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 42,
                            "status": "completed",
                            "conclusion": "success",
                            "secret": "not exposed",
                        }
                    ]
                }
            ),
            "",
        )

        runs = GitHubClient().workflow_runs("octo/project")

        self.assertEqual(runs[0]["id"], 42)
        self.assertEqual(runs[0]["conclusion"], "success")
        self.assertNotIn("secret", runs[0])
        self.assertEqual(
            run.call_args.args[0][2],
            "repos/octo/project/actions/runs?per_page=20",
        )

    @patch("aibridge.github.subprocess.run")
    def test_reads_logs_with_github_cli_and_truncates_large_output(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(["gh"], 0, "x" * 20, "")

        logs = GitHubClient(log_limit=10).workflow_logs("octo/project", "123")

        self.assertTrue(logs.startswith("x" * 10))
        self.assertIn("truncado", logs)
        self.assertEqual(run.call_args.args[0], ["gh", "run", "view", "123", "--repo", "octo/project", "--log"])

    @patch("aibridge.github.subprocess.run")
    def test_rejects_invalid_repository_before_running_command(self, run) -> None:
        with self.assertRaises(ValueError):
            GitHubClient().workflow_runs("owner/repo/extra")
        run.assert_not_called()

    @patch("aibridge.github.subprocess.run")
    def test_rejects_non_numeric_run_id_before_running_command(self, run) -> None:
        with self.assertRaises(ValueError):
            GitHubClient().workflow_logs("owner/repo", "../secret")
        run.assert_not_called()

    @patch("aibridge.github.subprocess.run", side_effect=FileNotFoundError)
    def test_handles_missing_github_cli(self, run) -> None:
        with self.assertRaisesRegex(GitHubUnavailable, "no está instalado"):
            GitHubClient().repositories()


if __name__ == "__main__":
    unittest.main()