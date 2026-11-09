import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from aibridge.agents import AgentEngine, TaskStore
from aibridge.dashboard import _dashboard_origins, create_server
from aibridge.providers import ProviderStatus
from aibridge.smart_router import SmartTaskEngine, SmartTaskStore


class FakeGitHubClient:
    def connection_status(self):
        return {"connected": False, "message": "No hay sesión de prueba."}

    def repositories(self):
        return [{"full_name": "octo/project", "private": True}]

    def workflow_runs(self, repository):
        return [{"id": 17, "status": "completed", "conclusion": "failure"}]

    def workflow_logs(self, repository, run_id):
        return f"logs para {repository} #{run_id}"


class TestSimulatedProvider:
    identifier = "simulation"
    display_name = "Simulación (solo pruebas)"

    def __init__(self):
        self.instructions = []

    def generate(self, instruction):
        self.instructions.append(instruction)
        return f"[SIMULACIÓN] {instruction}"


class BlockingSmartProvider:
    identifier = "simulation"
    display_name = "Simulación bloqueante"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.instructions: list[str] = []

    def generate(self, instruction: str) -> str:
        self.instructions.append(instruction)
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("La prueba no liberó el proveedor simulado.")
        return "[SIMULACIÓN] completed"


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "README.md").write_text("Local project\n", encoding="utf-8", newline="\n")
        (self.root / "main.py").write_text("print('hello')\n", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=value\n", encoding="utf-8")
        (self.root / ".git").mkdir()
        self.simulated_provider = None
        self.server = create_server(
            self.root, port=0, github_client=FakeGitHubClient()
        )
        self.server.RequestHandlerClass.agent_engine = AgentEngine(
            store=TaskStore(self.root / ".state" / "tasks.sqlite3")
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def enable_simulated_provider(self):
        self.simulated_provider = TestSimulatedProvider()
        simulated_status = ProviderStatus(
            identifier="simulation",
            display_name="Simulación (solo pruebas)",
            configured=True,
            enabled=True,
            simulation=True,
            message="Solo está disponible en pruebas automatizadas.",
        )
        self.server.RequestHandlerClass.agent_engine = AgentEngine(
            store=TaskStore(self.root / ".state" / "tasks.sqlite3"),
            provider=self.simulated_provider,
            provider_status=simulated_status,
            provider_statuses=(simulated_status,),
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary_directory.cleanup()

    def get_json(self, route: str, *, cookie: str | None = None):
        request = Request(
            self.base_url + route,
            headers={"Cookie": cookie} if cookie is not None else {},
        )
        with urlopen(request) as response:
            return json.loads(response.read()), response.headers

    def post_json(self, route: str, payload, *, origin=None, csrf=None, cookie=None, host=None):
        headers = {
            "Origin": self.base_url if origin is None else origin,
            "Content-Type": "application/json",
        }
        if host is not None:
            headers["Host"] = host
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        if cookie is not None:
            headers["Cookie"] = cookie
        request = Request(
            self.base_url + route,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return urlopen(request)

    def authorize_agent(self):
        session, headers = self.get_json("/api/session")
        csrf = session["csrf_token"]
        csrf_cookie = headers["Set-Cookie"].split(";", 1)[0]
        with self.post_json(
            "/api/agents/auth",
            {"access_code": self.server.authorization_token},
            csrf=csrf,
            cookie=csrf_cookie,
        ) as response:
            auth_cookies = response.headers.get_all("Set-Cookie")
            self.assertTrue(any("HttpOnly" in value for value in auth_cookies))
        auth_cookie = next(
            cookie.split(";", 1)[0]
            for cookie in auth_cookies
            if cookie.startswith("aibridge_session=")
        )
        return csrf, f"{csrf_cookie}; {auth_cookie}"

    def test_dashboard_serves_local_ui_with_security_headers(self) -> None:
        with urlopen(self.base_url + "/") as response:
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("AI-Bridge Control Center", page)
        self.assertIn('id="task-form"', page)
        self.assertIn("APPROVAL-GATED TASKS", page)
        with urlopen(self.base_url + "/assets/app.js") as response:
            script = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
        self.assertIn("/api/session", script)
        self.assertIn("/api/agents/auth", script)

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
        try:
            (self.root / "outside-link").symlink_to(outside)
        except (NotImplementedError, OSError) as error:
            outside.unlink(missing_ok=True)
            self.skipTest(f"Symlinks are unavailable on this platform: {error}")
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

    def test_agent_tasks_require_csrf_and_explicit_approval(self) -> None:
        self.enable_simulated_provider()
        csrf_data, session_headers = self.get_json("/api/session")
        csrf = csrf_data["csrf_token"]
        csrf_cookie = session_headers["Set-Cookie"].split(";", 1)[0]

        with self.assertRaises(HTTPError) as error:
            urlopen(
                Request(
                    self.base_url + "/api/agents",
                    headers={"Cookie": csrf_cookie},
                )
            )
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        with self.assertRaises(HTTPError) as error:
            self.post_json(
                "/api/agents/tasks",
                {"instruction": "Do not send before approval"},
                csrf=csrf,
                cookie=csrf_cookie,
            )
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        with self.assertRaises(HTTPError) as error:
            self.post_json(
                "/api/agents/tasks",
                {"instruction": "Missing CSRF"},
            )
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

        csrf, cookie = self.authorize_agent()
        with urlopen(
            Request(
                self.base_url + "/api/agents",
                headers={"Cookie": cookie},
            )
        ) as response:
            agent_status = json.loads(response.read())
        self.assertTrue(agent_status["provider"]["simulation"])

        with self.post_json(
            "/api/agents/tasks",
            {"instruction": "Summarize locally"},
            csrf=csrf,
            cookie=cookie,
        ) as response:
            task = json.loads(response.read())["task"]
            self.assertEqual(response.status, 201)
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertEqual(self.simulated_provider.instructions, [])

        with self.assertRaises(HTTPError) as error:
            self.post_json(
                f"/api/agents/tasks/{task['id']}/approve",
                {},
                csrf=csrf,
                cookie=cookie.split(";", 1)[0],
            )
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        with self.post_json(
            f"/api/agents/tasks/{task['id']}/approve",
            {},
            csrf=csrf,
            cookie=cookie,
        ) as response:
            approved = json.loads(response.read())["task"]
        self.assertEqual(approved["status"], "completed")
        self.assertEqual(approved["result"], "[SIMULACIÓN] Summarize locally")
        self.assertEqual(self.simulated_provider.instructions, ["Summarize locally"])

    def test_agent_mutations_reject_a_cross_origin_request(self) -> None:
        session, headers = self.get_json("/api/session")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        with self.assertRaises(HTTPError) as error:
            self.post_json(
                "/api/agents/tasks",
                {"instruction": "Cross-origin"},
                origin="https://attacker.example",
                csrf=session["csrf_token"],
                cookie=cookie,
            )
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_unconfigured_provider_keeps_approved_tasks_pending(self) -> None:
        session, headers = self.get_json("/api/session")
        csrf = session["csrf_token"]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        with self.assertRaises(HTTPError) as error:
            self.post_json(
                "/api/agents/auth",
                {"access_code": "incorrect-access-code"},
                csrf=csrf,
                cookie=cookie,
            )
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        csrf, cookie = self.authorize_agent()
        with self.post_json(
            "/api/agents/tasks",
            {"instruction": "No provider call should happen"},
            csrf=csrf,
            cookie=cookie,
        ) as response:
            task = json.loads(response.read())["task"]

        with self.assertRaises(HTTPError) as error:
            self.post_json(
                f"/api/agents/tasks/{task['id']}/approve",
                {},
                csrf=csrf,
                cookie=cookie,
            )
        self.assertEqual(error.exception.code, 503)
        error.exception.close()
        stored = self.server.RequestHandlerClass.agent_engine.store.get(task["id"])
        self.assertEqual(stored.status, "awaiting_approval")

    def test_agent_engine_is_shared_while_http_task_is_running(self) -> None:
        provider = BlockingSmartProvider()
        status = ProviderStatus(
            identifier="simulation",
            display_name="Simulación bloqueante",
            configured=True,
            enabled=True,
            simulation=False,
            message="Solo está disponible en pruebas automatizadas.",
            model="simulation-test-model",
            availability="available",
        )
        database_path = self.root / ".state" / "smart-tasks.sqlite3"
        engines: list[SmartTaskEngine] = []

        def create_test_engine() -> SmartTaskEngine:
            engine = SmartTaskEngine(
                SmartTaskStore(database_path),
                {"simulation": provider},
                (status,),
                (),
            )
            engines.append(engine)
            return engine

        self.server.RequestHandlerClass.agent_engine = None
        csrf, cookie = self.authorize_agent()
        try:
            with patch("aibridge.dashboard.create_smart_engine", create_test_engine):
                with self.post_json(
                    "/api/agents/tasks",
                    {"instruction": "Keep this task running"},
                    csrf=csrf,
                    cookie=cookie,
                ) as response:
                    task = json.loads(response.read())["task"]

                with self.post_json(
                    f"/api/agents/tasks/{task['id']}/approve",
                    {"provider_ids": ["simulation"], "external_consent": True},
                    csrf=csrf,
                    cookie=cookie,
                ) as response:
                    self.assertEqual(response.status, 202)

                self.assertTrue(provider.started.wait(timeout=2))
                observed_statuses = []
                for _ in range(5):
                    payload, _ = self.get_json("/api/agents", cookie=cookie)
                    observed_statuses.append(payload["tasks"][0]["status"])
                self.assertEqual(observed_statuses, ["running"] * 5)

                provider.release.set()
                for _ in range(100):
                    payload, _ = self.get_json("/api/agents", cookie=cookie)
                    if payload["tasks"][0]["status"] == "completed":
                        break
                    time.sleep(0.02)
                self.assertEqual(payload["tasks"][0]["status"], "completed")
                self.assertEqual(
                    payload["tasks"][0]["result"], "[SIMULACIÓN] completed"
                )
                self.assertEqual(provider.instructions, ["Keep this task running"])
                self.assertEqual(len(engines), 1)
                self.assertIs(
                    self.server.RequestHandlerClass.agent_engine, engines[0]
                )
        finally:
            provider.release.set()
            for engine in engines:
                engine.stop_worker()

    def test_dashboard_rejects_untrusted_host_headers(self) -> None:
        malicious_host = "attacker.example"
        request = Request(self.base_url + "/", headers={"Host": malicious_host})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_codespaces_origin_uses_forwarding_domain_and_port(self) -> None:
        environment = {
            "CODESPACES": "true",
            "CODESPACE_NAME": "test-space",
            "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "ports.example.test",
            "AI_BRIDGE_ALLOWED_ORIGINS": "https://*.attacker.example",
        }
        with patch.dict("os.environ", environment):
            origins = _dashboard_origins("0.0.0.0", 8000)

        self.assertIn("https://test-space-8000.ports.example.test", origins)
        self.assertNotIn("https://test-space-8000.ports.example.test:8000", origins)
        self.assertNotIn("http://test-space-8000.ports.example.test", origins)
        self.assertNotIn("https://test-space-8000.app.github.dev", origins)
        self.assertNotIn("https://*.attacker.example", origins)
        self.assertIn("http://localhost:8000", origins)
        self.assertIn("http://127.0.0.1:8000", origins)
        self.assertIn("http://[::1]:8000", origins)

    def test_codespaces_http_accepts_only_forwarded_host_and_origin(self) -> None:
        environment = {
            "CODESPACES": "true",
            "CODESPACE_NAME": "test-space",
            "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "ports.example.test",
        }
        token = "test-session-token"
        with patch.dict("os.environ", environment):
            server = create_server(
                self.root,
                host="127.0.0.1",
                port=0,
                github_client=FakeGitHubClient(),
                authorization_token=token,
            )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        forwarded_host = (
            f"test-space-{server.server_port}.ports.example.test"
        )
        forwarded_origin = f"https://{forwarded_host}"
        server_url = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(server_url + "/", headers={"Host": forwarded_host})
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)

            session_request = Request(
                server_url + "/api/session",
                headers={"Host": forwarded_host, "X-Forwarded-Proto": "https"},
            )
            with urlopen(session_request) as response:
                session = json.loads(response.read())
                csrf_cookie = response.headers["Set-Cookie"].split(";", 1)[0]

            auth_request = Request(
                server_url + "/api/agents/auth",
                data=json.dumps({"access_code": token}).encode("utf-8"),
                headers={
                    "Host": forwarded_host,
                    "Origin": forwarded_origin,
                    "Content-Type": "application/json",
                    "Cookie": csrf_cookie,
                    "X-CSRF-Token": session["csrf_token"],
                    "X-Forwarded-Proto": "https",
                },
                method="POST",
            )
            with urlopen(auth_request) as response:
                self.assertEqual(response.status, 200)

            forged_host_request = Request(
                server_url + "/",
                headers={"Host": f"test-space-{server.server_port}.attacker.example"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(forged_host_request)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()

            port_injected_host_request = Request(
                server_url + "/",
                headers={"Host": f"{forwarded_host}:{server.server_port}"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(port_injected_host_request)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()

            forged_origin_request = Request(
                server_url + "/api/agents/auth",
                data=json.dumps({"access_code": token}).encode("utf-8"),
                headers={
                    "Host": forwarded_host,
                    "Origin": f"http://{forwarded_host}",
                    "Content-Type": "application/json",
                    "Cookie": csrf_cookie,
                    "X-CSRF-Token": session["csrf_token"],
                },
                method="POST",
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(forged_origin_request)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_server_rejects_public_and_wildcard_allowed_origins(self) -> None:
        for origin in ("https://attacker.example", "https://*.example.test"):
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    create_server(
                        self.root,
                        port=0,
                        allowed_origins=frozenset({origin}),
                    )

    def test_task_creation_rejects_malformed_and_oversized_requests(self) -> None:
        csrf, cookie = self.authorize_agent()
        malformed = Request(
            self.base_url + "/api/agents/tasks",
            data=b"{not-json",
            headers={
                "Origin": self.base_url,
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
                "Cookie": cookie,
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(malformed)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

        oversized = Request(
            self.base_url + "/api/agents/tasks",
            data=b"x" * 64_001,
            headers={
                "Origin": self.base_url,
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
                "Cookie": cookie,
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(oversized)
        self.assertEqual(error.exception.code, 413)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()