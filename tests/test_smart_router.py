import json
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
from urllib.request import Request, urlopen

from aibridge.agents import AgentUnavailable, TaskValidationError
from aibridge.providers import (
    CloudflareWorkersAIProvider,
    GeminiProvider,
    GroqProvider,
    OpenRouterProvider,
    ProviderError,
    ProviderStatus,
    configure_free_providers,
)
from aibridge.router import ProviderHealth, RouterSettings, classify_task, rank_providers
from aibridge.smart_router import SmartTaskEngine, SmartTaskStore
from aibridge.dashboard import create_server


class FakeProvider:
    def __init__(self, identifier, outcome):
        self.identifier = identifier
        self.display_name = identifier
        self.outcome = outcome
        self.calls = []

    def generate(self, instruction):
        self.calls.append(instruction)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def provider_status(identifier, *, enabled=True, model=None):
    return ProviderStatus(
        identifier=identifier,
        display_name=identifier.title(),
        configured=True,
        enabled=enabled,
        simulation=False,
        message="Free-tier test provider.",
        model=model or f"{identifier}-test-model",
        availability="available" if enabled else "disabled",
        quota="Test only.",
    )


class SmartRouterTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "tasks.sqlite3"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def make_engine(self, providers, statuses):
        engine = SmartTaskEngine(
            SmartTaskStore(self.database_path),
            providers,
            tuple(statuses),
            sensitive_values=("api-key-sentinel",),
        )
        engine.store.save_router_settings(
            RouterSettings(
                enabled_provider_ids=tuple(status.identifier for status in statuses),
                priorities={statuses[0].identifier: 10},
            )
        )
        return engine

    def test_classifies_task_without_claiming_model_quality(self):
        self.assertEqual(classify_task("Implement a Python function"), "programming")
        self.assertEqual(classify_task("Analyze the tradeoffs"), "reasoning")
        self.assertEqual(classify_task("Summarize this note"), "general")

    def test_routing_honors_capabilities_context_and_manual_fallback_order(self):
        limited_context = ProviderStatus(
            **{
                **provider_status("small").__dict__,
                "context_window_tokens": 400,
            }
        )
        capable = ProviderStatus(
            **{
                **provider_status("preferred").__dict__,
                "capabilities": ("text", "reasoning"),
                "context_window_tokens": 4_096,
            }
        )
        backup = ProviderStatus(
            **{
                **provider_status("backup").__dict__,
                "context_window_tokens": 4_096,
            }
        )
        no_text = ProviderStatus(
            **{
                **provider_status("not-text").__dict__,
                "capabilities": ("image",),
            }
        )
        candidates = rank_providers(
            "Analyze the design tradeoffs",
            (limited_context, capable, no_text, backup),
            RouterSettings(
                mode="manual",
                manual_provider_id="preferred",
                enabled_provider_ids=("small", "preferred", "not-text", "backup"),
            ),
            {"preferred": ProviderHealth()},
            now=0,
        )
        self.assertEqual(
            [item.provider_id for item in candidates], ["preferred", "backup"]
        )

    def test_documented_task_capability_precedes_unverified_model_for_code(self):
        gemini = ProviderStatus(
            **{
                **provider_status("gemini", model="gemini-3.8-flash").__dict__,
                "capabilities": ("text", "programming", "reasoning"),
                "context_window_tokens": 1_048_576,
            }
        )
        groq = ProviderStatus(
            **{
                **provider_status("groq", model="openai/gpt-oss-120b").__dict__,
                "capabilities": ("text", "programming", "reasoning"),
                "context_window_tokens": 131_072,
            }
        )
        openrouter = provider_status("openrouter", model="openrouter/free")
        candidates = rank_providers(
            "Implement and analyze this code",
            (openrouter, groq, gemini),
            RouterSettings(
                enabled_provider_ids=("openrouter", "groq", "gemini")
            ),
            {},
            now=0,
        )
        self.assertEqual(
            {candidate.provider_id for candidate in candidates[:2]},
            {"gemini", "groq"},
        )
        self.assertIn("no medición comparativa", candidates[0].explanation)

    def test_approval_is_persisted_and_execution_is_asynchronous(self):
        provider = FakeProvider("first", "response")
        engine = self.make_engine({"first": provider}, [provider_status("first")])
        task = engine.create_task("Summarize a project")
        self.assertEqual(task.status, "awaiting_approval")
        self.assertEqual(provider.calls, [])

        with self.assertRaises(TaskValidationError):
            engine.approve_task(task.id, provider_ids=["first"])
        approved = engine.approve_task(
            task.id, provider_ids=["first"], external_consent=True
        )
        self.assertEqual(approved.status, "approved")
        self.assertEqual(approved.allowed_provider_ids, ("first",))
        self.assertEqual(provider.calls, [])

        self.assertTrue(engine.run_next_task())
        completed = engine.store.get(task.id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.result, "response")
        self.assertEqual(completed.provider_id, "first")

    def test_falls_back_after_http_429_and_persists_reason(self):
        first = FakeProvider(
            "first", ProviderError("HTTP 429", kind="rate_limited", http_status=429)
        )
        second = FakeProvider("second", "fallback response")
        engine = self.make_engine(
            {"first": first, "second": second},
            [provider_status("first"), provider_status("second")],
        )
        task = engine.create_task("Analyze this code")
        engine.approve_task(
            task.id,
            provider_ids=["first", "second"],
            external_consent=True,
        )
        engine.run_next_task()

        stored = engine.store.get(task.id)
        self.assertEqual(stored.status, "completed")
        self.assertEqual(stored.provider_id, "second")
        self.assertEqual(first.calls, ["Analyze this code"])
        self.assertEqual(second.calls, ["Analyze this code"])
        attempts = engine.store.attempt_history()
        self.assertEqual(attempts[0]["outcome"], "completed")
        self.assertIn("Tarea programming", attempts[0]["reason"])
        self.assertIn("HTTP 429", attempts[1]["reason"])
        health = {
            entry["provider_id"]: entry for entry in engine.public_status()["health"]
        }
        self.assertEqual(health["first"]["state"], "cooldown")

    def test_falls_back_after_timeout_but_not_after_invalid_request(self):
        timeout = FakeProvider(
            "first", ProviderError("timeout", kind="transient")
        )
        second = FakeProvider("second", "ok")
        engine = self.make_engine(
            {"first": timeout, "second": second},
            [provider_status("first"), provider_status("second")],
        )
        task = engine.create_task("Generate a small summary")
        engine.approve_task(
            task.id, provider_ids=["first", "second"], external_consent=True
        )
        engine.run_next_task()
        self.assertEqual(engine.store.get(task.id).provider_id, "second")
        self.assertEqual(len(second.calls), 1)

        invalid = FakeProvider(
            "first", ProviderError("invalid input", kind="invalid_request")
        )
        untouched = FakeProvider("second", "must not run")
        engine = self.make_engine(
            {"first": invalid, "second": untouched},
            [provider_status("first"), provider_status("second")],
        )
        task = engine.create_task("Invalid request")
        engine.approve_task(
            task.id, provider_ids=["first", "second"], external_consent=True
        )
        engine.run_next_task()
        self.assertEqual(engine.store.get(task.id).status, "failed")
        self.assertEqual(untouched.calls, [])

    def test_all_providers_failing_preserves_task_for_explicit_retry(self):
        first = FakeProvider("first", ProviderError("temporary", kind="transient"))
        second = FakeProvider("second", ProviderError("unavailable", kind="transient"))
        engine = self.make_engine(
            {"first": first, "second": second},
            [provider_status("first"), provider_status("second")],
        )
        task = engine.create_task("Retry later")
        engine.approve_task(
            task.id, provider_ids=["first", "second"], external_consent=True
        )
        engine.run_next_task()

        stored = engine.store.get(task.id)
        self.assertEqual(stored.status, "failed")
        self.assertIn("unavailable", stored.error)
        self.assertEqual(len(engine.store.attempt_history()), 2)

    def test_credentials_error_disables_provider_and_does_not_fallback(self):
        invalid_key = FakeProvider(
            "first", ProviderError("unauthorized", kind="credentials", http_status=401)
        )
        second = FakeProvider("second", "no fallback")
        engine = self.make_engine(
            {"first": invalid_key, "second": second},
            [provider_status("first"), provider_status("second")],
        )
        task = engine.create_task("Check credentials")
        engine.approve_task(
            task.id, provider_ids=["first", "second"], external_consent=True
        )
        engine.run_next_task()
        state = engine.public_status()
        first_status = next(item for item in state["providers"] if item["id"] == "first")
        self.assertFalse(first_status["configured"])
        self.assertFalse(first_status["enabled"])
        self.assertEqual(second.calls, [])

    def test_paid_or_unverified_models_are_disabled_by_default(self):
        config = configure_free_providers(
            {
                "AI_BRIDGE_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "router-key",
                "AI_BRIDGE_OPENROUTER_MODEL": "provider/paid-model",
                "AI_BRIDGE_ALLOW_PAID_API": "true",
                "GEMINI_API_KEY": "gemini-key",
                "AI_BRIDGE_GEMINI_MODEL": "gemini-3.8-flash",
            }
        )
        self.assertTrue(config.free_only)
        self.assertNotIn("openrouter", config.providers)
        self.assertNotIn("gemini", config.providers)
        statuses = {status.identifier: status for status in config.statuses}
        self.assertFalse(statuses["openrouter"].enabled)
        self.assertFalse(statuses["gemini"].enabled)

    def test_documented_free_routes_require_verified_account_tier(self):
        env = {
            "GEMINI_API_KEY": "gemini-key",
            "AI_BRIDGE_GEMINI_MODEL": "gemini-3.8-flash",
            "AI_BRIDGE_GEMINI_FREE_TIER_CONFIRMED": "true",
            "GROQ_API_KEY": "groq-key",
            "AI_BRIDGE_GROQ_MODEL": "openai/gpt-oss-20b",
            "AI_BRIDGE_GROQ_FREE_TIER_CONFIRMED": "true",
            "OPENROUTER_API_KEY": "router-key",
            "AI_BRIDGE_OPENROUTER_MODEL": "openrouter/free",
            "CLOUDFLARE_API_TOKEN": "cf-token",
            "CLOUDFLARE_ACCOUNT_ID": "account-123",
            "AI_BRIDGE_CLOUDFLARE_FREE_PLAN_CONFIRMED": "true",
            "AI_BRIDGE_CLOUDFLARE_DAILY_NEURON_BUDGET": "8001",
        }
        config = configure_free_providers(env)
        self.assertEqual(
            set(config.providers), {"gemini", "groq", "openrouter"}
        )
        statuses = {status.identifier: status for status in config.statuses}
        self.assertFalse(statuses["cloudflare"].enabled)
        self.assertIn("8000", statuses["cloudflare"].message)
        self.assertEqual(statuses["gemini"].context_window_tokens, 1_048_576)
        self.assertEqual(statuses["groq"].context_window_tokens, 131_072)
        self.assertIn("reasoning", statuses["gemini"].capabilities)
        self.assertNotIn("gemini-key", json.dumps(config.statuses, default=lambda item: item.__dict__))

    def test_interrupted_tasks_are_failed_not_replayed_and_history_survives(self):
        provider = FakeProvider("first", "unused")
        engine = self.make_engine({"first": provider}, [provider_status("first")])
        task = engine.create_task("Recover safely")
        engine.approve_task(
            task.id, provider_ids=["first"], external_consent=True
        )
        self.assertEqual(engine.store.claim_next().status, "running")

        restarted = SmartTaskEngine(
            SmartTaskStore(self.database_path),
            {"first": provider},
            (provider_status("first"),),
            (),
        )
        recovered = restarted.store.get(task.id)
        self.assertEqual(recovered.status, "failed")
        self.assertIn("vuelve a aprobarla", recovered.error)
        retried = restarted.approve_task(
            task.id, provider_ids=["first"], external_consent=True
        )
        self.assertEqual(retried.status, "approved")
        self.assertEqual(restarted.task_history()[0].id, task.id)

    def test_interrupted_provider_request_is_not_sent_again_after_restart(self):
        provider = FakeProvider("first", "uncertain response")
        engine = self.make_engine({"first": provider}, [provider_status("first")])
        task = engine.create_task("Do not duplicate an uncertain request")
        engine.approve_task(
            task.id, provider_ids=["first"], external_consent=True
        )

        running = engine.store.claim_next()
        self.assertIsNotNone(running)
        provider.generate(task.instruction)
        self.assertEqual(provider.calls, [task.instruction])

        restarted = SmartTaskEngine(
            SmartTaskStore(self.database_path),
            {"first": provider},
            (provider_status("first"),),
            (),
        )
        try:
            recovered = restarted.store.get(task.id)
            self.assertEqual(recovered.status, "failed")
            self.assertIn("vuelve a aprobarla", recovered.error)
            self.assertEqual(provider.calls, [task.instruction])
        finally:
            engine.stop_worker()
            restarted.stop_worker()

    def test_approved_tasks_resume_after_restart(self):
        provider = FakeProvider("first", "resumed response")
        engine = self.make_engine({"first": provider}, [provider_status("first")])
        task = engine.create_task("Resume an approved task")
        engine.approve_task(
            task.id, provider_ids=["first"], external_consent=True
        )

        restarted = SmartTaskEngine(
            SmartTaskStore(self.database_path),
            {"first": provider},
            (provider_status("first"),),
            (),
        )
        try:
            for _ in range(50):
                recovered = restarted.store.get(task.id)
                if recovered.status == "completed":
                    break
                time.sleep(0.02)
            self.assertEqual(recovered.status, "completed")
            self.assertEqual(recovered.result, "resumed response")
            self.assertEqual(provider.calls, ["Resume an approved task"])
        finally:
            restarted.stop_worker()

    def test_local_request_and_token_limits_block_outbound_calls(self):
        provider = FakeProvider("first", "unused")
        engine = self.make_engine({"first": provider}, [provider_status("first")])
        task = engine.create_task("Limited")
        engine.store.reserve("first", 20_000)
        with self.assertRaises(AgentUnavailable):
            engine.approve_task(
                task.id, provider_ids=["first"], external_consent=True
            )
        self.assertEqual(provider.calls, [])
        self.assertEqual(engine.store.get(task.id).status, "awaiting_approval")

    def test_http_statuses_are_classified_for_bounded_fallback(self):
        cases = ((429, "rate_limited"), (503, "transient"), (401, "credentials"), (400, "invalid_request"))
        for status_code, expected_kind in cases:
            with self.subTest(status=status_code):
                error = HTTPError(
                    "https://api.example.test",
                    status_code,
                    "error",
                    {},
                    BytesIO(b"{}"),
                )
                with patch("aibridge.providers.urlopen", side_effect=error):
                    with self.assertRaises(ProviderError) as raised:
                        from aibridge.providers import GeminiProvider

                        GeminiProvider("key", "gemini-3.8-flash").generate("hello")
                self.assertEqual(raised.exception.kind, expected_kind)

    def test_new_connectors_use_official_fixed_endpoints_and_keep_keys_out_of_payload(self):
        responses = (
            (
                GeminiProvider("secret", "gemini-3.8-flash"),
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",
                {"candidates": [{"content": {"parts": [{"text": "gemini"}]}}]},
                "x-goog-api-key",
            ),
            (
                GroqProvider("secret", "openai/gpt-oss-20b"),
                "https://api.groq.com/openai/v1/chat/completions",
                {"choices": [{"message": {"content": "groq"}}]},
                "Authorization",
            ),
            (
                OpenRouterProvider("secret", "openrouter/free"),
                "https://openrouter.ai/api/v1/chat/completions",
                {"model": "actual-free/model", "choices": [{"message": {"content": "openrouter"}}]},
                "Authorization",
            ),
            (
                CloudflareWorkersAIProvider(
                    "secret", "account-123", "@cf/meta/llama-3.2-1b-instruct"
                ),
                "https://api.cloudflare.com/client/v4/accounts/account-123/ai/run/@cf/meta/llama-3.2-1b-instruct",
                {"result": {"response": "cloudflare"}},
                "Authorization",
            ),
        )
        class FakeResponse:
            def __init__(self, value):
                self.body = json.dumps(value).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return self.body

        for provider, endpoint, payload, credential_header in responses:
            with self.subTest(provider=provider.identifier):
                with patch(
                    "aibridge.providers.urlopen", return_value=FakeResponse(payload)
                ) as mocked_urlopen:
                    self.assertTrue(provider.generate("hello"))
                request = mocked_urlopen.call_args.args[0]
                self.assertEqual(request.full_url, endpoint)
                self.assertNotIn("secret", request.data.decode())
                header = next(
                    value
                    for name, value in request.headers.items()
                    if name.lower() == credential_header.lower()
                )
                self.assertEqual(
                    header,
                    "secret"
                    if credential_header.lower() == "x-goog-api-key"
                    else "Bearer secret",
                )


class SmartRouterDashboardSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.provider = FakeProvider("gemini", "safe test result")
        self.engine = SmartTaskEngine(
            SmartTaskStore(self.root / "tasks.sqlite3"),
            {"gemini": self.provider},
            (provider_status("gemini", model="gemini-3.8-flash"),),
            (),
        )
        self.server = create_server(
            self.root,
            port=0,
            agent_engine=self.engine,
            authorization_token="test-access-code",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.engine.stop_worker()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary_directory.cleanup()

    def get_json(self, path):
        with urlopen(self.base_url + path) as response:
            return json.loads(response.read()), response.headers

    def post_json(self, path, payload, *, csrf=None, cookie=None):
        headers = {
            "Origin": self.base_url,
            "Content-Type": "application/json",
        }
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if cookie:
            headers["Cookie"] = cookie
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        return urlopen(request)

    def test_router_and_plan_endpoints_require_authentication_and_csrf(self):
        session, headers = self.get_json("/api/session")
        csrf = session["csrf_token"]
        csrf_cookie = headers["Set-Cookie"].split(";", 1)[0]
        for path, payload in (
            ("/api/agents/router", {"mode": "automatic", "enabled_provider_ids": ["gemini"]}),
            ("/api/agents/plan", {"instruction": "Analyze code"}),
        ):
            with self.assertRaises(HTTPError) as error:
                self.post_json(path, payload, csrf=csrf, cookie=csrf_cookie)
            self.assertEqual(error.exception.code, 401)
            error.exception.close()

        auth_response = self.post_json(
            "/api/agents/auth",
            {"access_code": "test-access-code"},
            csrf=csrf,
            cookie=csrf_cookie,
        )
        auth_cookies = auth_response.headers.get_all("Set-Cookie")
        auth_response.close()
        cookie = "; ".join(
            [csrf_cookie]
            + [value.split(";", 1)[0] for value in auth_cookies]
        )
        with self.assertRaises(HTTPError) as error:
            self.post_json(
                "/api/agents/plan",
                {"instruction": "Analyze the tradeoffs"},
                cookie=cookie,
            )
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

        with self.post_json(
            "/api/agents/plan",
            {"instruction": "Analyze the tradeoffs"},
            csrf=csrf,
            cookie=cookie,
        ) as response:
            plan = json.loads(response.read())
        self.assertEqual(plan["task_type"], "reasoning")
        self.assertEqual(plan["candidates"][0]["provider_id"], "gemini")
        with self.post_json(
            "/api/agents/router",
            {
                "mode": "manual",
                "manual_provider_id": "gemini",
                "enabled_provider_ids": ["gemini"],
                "priorities": {"gemini": 4},
            },
            csrf=csrf,
            cookie=cookie,
        ) as response:
            settings = json.loads(response.read())["settings"]
        self.assertEqual(settings["mode"], "manual")
        self.assertEqual(settings["priorities"]["gemini"], 4)

    def test_api_approval_requires_data_consent_and_queues_work(self):
        session, headers = self.get_json("/api/session")
        csrf = session["csrf_token"]
        csrf_cookie = headers["Set-Cookie"].split(";", 1)[0]
        auth_response = self.post_json(
            "/api/agents/auth",
            {"access_code": "test-access-code"},
            csrf=csrf,
            cookie=csrf_cookie,
        )
        auth_cookies = auth_response.headers.get_all("Set-Cookie")
        auth_response.close()
        cookie = "; ".join(
            [csrf_cookie] + [value.split(";", 1)[0] for value in auth_cookies]
        )
        with self.post_json(
            "/api/agents/tasks",
            {"instruction": "Summarize this project"},
            csrf=csrf,
            cookie=cookie,
        ) as response:
            task = json.loads(response.read())["task"]
        self.assertEqual(task["status"], "awaiting_approval")

        with self.assertRaises(HTTPError) as error:
            self.post_json(
                f"/api/agents/tasks/{task['id']}/approve",
                {"provider_ids": ["gemini"], "external_consent": False},
                csrf=csrf,
                cookie=cookie,
            )
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(self.provider.calls, [])

        with self.post_json(
            f"/api/agents/tasks/{task['id']}/approve",
            {"provider_ids": ["gemini"], "external_consent": True},
            csrf=csrf,
            cookie=cookie,
        ) as response:
            approved = json.loads(response.read())["task"]
            self.assertEqual(response.status, 202)
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["allowed_provider_ids"], ["gemini"])
        for _ in range(50):
            if self.engine.store.get(task["id"]).status == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(self.engine.store.get(task["id"]).status, "completed")


if __name__ == "__main__":
    unittest.main()
