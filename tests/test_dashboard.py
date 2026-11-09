import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from aibridge.dashboard import create_server


class FakeGitHubClient:
    def connection_status(self):
        return {"connected": False, "message": "No hay sesión de prueba."}

    def repositories(self):
        return [{"full_name": "octo/project", "private": True}]

    def workflow_runs(self, repository):
        return [{"id": 17, "status": "completed", "conclusion": "failure"}]

    def workflow_logs(self, repository, run_id):
        return f"logs para {repository} #{run_id}"


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "README.md").write_text("Local project\n", encoding="utf-8")
        (self.root / "main.py").write_text("print('hello')\n", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=value\n", encoding="utf-8")
        (self.root / ".git").mkdir()
        self.server = create_server(
            self.root, port=0, github_client=FakeGitHubClient()
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary_directory.cleanup()

    def get_json(self, route: str):
        with urlopen(self.base_url + route) as response:
            return json.loads(response.read()), response.headers

    def test_dashboard_serves_local_ui_with_security_headers(self) -> None:
        with urlopen(self.base_url + "/") as response:
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("AI-Bridge Control Center", page)

    def test_status_uses_inspector_and_marks_unconfigured_services(self) -> None:
        payload, _ = self.get_json("/api/status")

        self.assertEqual(payload["application"], "online")
        self.assertEqual(payload["repository"]["name"], self.root.name)
        self.assertFalse(payload["github"]["connected"])
        self.assertFalse(payload["agents"]["configured"])

    def test_lists_files_without_secrets_or_git_metadata(self) -> None:
        payload, _ = self.get_json("/api/files")

        names = {entry["name"] for entry in payload["entries"]}
        self.assertIn("README.md", names)
        self.assertIn("main.py", names)
        self.assertNotIn(".env", names)
        self.assertNotIn(".git", names)

    def test_reads_text_file_and_blocks_traversal_and_secrets(self) -> None:
        payload, _ = self.get_json("/api/file?" + urlencode({"path": "README.md"}))
        self.assertEqual(payload["content"], "Local project\n")

        for path, expected_status in (("../outside", 400), (".env", 403)):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as error:
                    urlopen(self.base_url + "/api/file?" + urlencode({"path": path}))
                self.assertEqual(error.exception.code, expected_status)
                error.exception.close()

    def test_blocks_symlinks_that_point_outside_the_workspace(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.txt"
        outside.write_text("private\n", encoding="utf-8")
        (self.root / "outside-link").symlink_to(outside)
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(
                    self.base_url
                    + "/api/file?"
                    + urlencode({"path": "outside-link"})
                )
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        finally:
            outside.unlink(missing_ok=True)

    def test_github_routes_return_adapter_data(self) -> None:
        repositories, _ = self.get_json("/api/repositories")
        runs, _ = self.get_json("/api/actions?repo=octo%2Fproject")
        logs, _ = self.get_json("/api/action-logs?repo=octo%2Fproject&run_id=17")

        self.assertEqual(repositories["repositories"][0]["full_name"], "octo/project")
        self.assertEqual(runs["runs"][0]["id"], 17)
        self.assertEqual(logs["logs"], "logs para octo/project #17")

    def test_unknown_api_route_is_rejected(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/api/unknown")
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()