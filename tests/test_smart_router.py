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
    model_route_id,
    provider_request_timeout,
)
from aibridge.router import (
    MAX_FALLBACK_ROUTES,
    ProviderHealth,
    RouterSettings,
    classify_task,
    rank_providers,
)
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
        paid = ProviderStatus(
            **{
                **provider_status("paid").__dict__,
                "free_only": False,
            }
        )
        candidates = rank_providers(
            "Analyze the design tradeoffs",
            (limited_context, capable, no_text, paid, backup),
            RouterSettings(
                mode="manual",
                manual_provider_id="preferred",
                enabled_provider_ids=("small", "preferred", "not-text", "paid", "backup"),
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
        self.assertEqual(
            approved.allowed_provider_ids,
            (model_route_id("first", "first-test-model"),),
        )
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

    def test_safety_rejection_does_not_fall_back_and_empty_text_is_not_completed(self):
        safety = FakeProvider(
            "first", ProviderError("blocked by safety policy", kind="safety_rejection")
        )
        untouched = FakeProvider("second", "must not run")
        engine = self.make_engine(
            {"first": safety, "second": untouched},
            [provider_status("first"), provider_status("second")],
        )
        task = engine.create_task("Do not bypass a safety restriction")
        engine.approve_task(
            task.id,
            provider_ids=["first", "second"],
            external_consent=True,
        )
        engine.run_next_task()
        stored = engine.store.get(task.id)
        self.assertEqual(stored.status, "failed")
        self.assertIn("safety_rejection", stored.error)
        self.assertEqual(untouched.calls, [])

        empty = FakeProvider("first", "")
        backup = FakeProvider("second", "valid response")
        engine = self.make_engine(
            {"first": empty, "second": backup},
            [provider_status("first"), provider_status("second")],
        )
        task = engine.create_task("Reject empty provider output")
        engine.approve_task(
            task.id,
            provider_ids=["first", "second"],
            external_consent=True,
        )
        engine.run_next_task()
        stored = engine.store.get(task.id)
        self.assertEqual(stored.status, "completed")
        self.assertEqual(stored.result, "valid response")
        self.assertEqual(empty.calls, [task.instruction])

        store = SmartTaskStore(self.database_path)
        pending = store.create("Store must reject empty completion")
        store.approve(pending.id, ["first"], external_consent=True)
        running = store.claim_next()
        self.assertIsNotNone(running)
        with self.assertRaises(TaskValidationError):
            store.complete(running.id)

    def test_all_providers_failing_preserves_task_for_explicit_retry(self):
        first = FakeProvider("first", ProviderError("temporary", kind="transient"))
        second = FakeProvider("second", ProviderError("unavailable", kind="transient"))
        unapproved = FakeProvider("unapproved", "must not run")
        unavailable_status = ProviderStatus(
            **{
                **provider_status("unavailable", enabled=False).__dict__,
                "message": "Falta la clave API.",
            }
        )
        engine = self.make_engine(
            {"first": first, "second": second, "unapproved": unapproved},
            [
                provider_status("first"),
                provider_status("second"),
                provider_status("unapproved"),
                unavailable_status,
            ],
        )
        task = engine.create_task("Retry later")
        engine.approve_task(
            task.id, provider_ids=["first", "second"], external_consent=True
        )
        engine.run_next_task()

        stored = engine.store.get(task.id)
        self.assertEqual(stored.status, "failed")
        self.assertIn("unavailable", stored.error)
        self.assertIn("Diagnóstico de rutas aprobadas:", stored.error)
        self.assertIn("first (first-test-model): transient", stored.error)
        self.assertIn("second (second-test-model): transient", stored.error)
        self.assertIn("unapproved (unapproved-test-model)", stored.error)
        self.assertIn("no autorizada para esta tarea", stored.error)
        self.assertIn("unavailable (unavailable-test-model): Falta la clave API.", stored.error)
        self.assertEqual(unapproved.calls, [])
        self.assertEqual(len(engine.store.attempt_history()), 2)

    def test_falls_back_between_models_within_one_provider(self):
        first_model = "openai/gpt-oss-20b"
        second_model = "openai/gpt-oss-120b"
        first = FakeProvider(
            "groq",
            ProviderError("temporary model failure", kind="transient"),
        )
        second = FakeProvider("groq", "second model response")
        providers = {
            model_route_id("groq", first_model): first,
            model_route_id("groq", second_model): second,
        }
        statuses = [
            provider_status("groq", model=first_model),
            provider_status("groq", model=second_model),
        ]
        engine = self.make_engine(providers, statuses)
        task = engine.create_task("Try registered Groq models")
        plan = engine.route_plan(task.instruction)
        engine.approve_task(
            task.id,
            provider_ids=[candidate.route_id for candidate in plan],
            external_consent=True,
        )

        engine.run_next_task()

        stored = engine.store.get(task.id)
        self.assertEqual(stored.status, "completed")
        self.assertEqual(stored.result, "second model response")
        self.assertEqual(stored.provider_id, "groq")
        self.assertEqual(stored.selected_model, second_model)
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)
        self.assertEqual(len(engine.store.attempt_history()), 2)

    def test_three_failed_model_routes_fall_back_to_the_fourth_without_looping(self):
        groq_first = "openai/gpt-oss-20b"
        groq_second = "openai/gpt-oss-120b"
        gemini_model = "gemini-3.8-flash"
        cloudflare_model = "@cf/meta/llama-3.2-1b-instruct"
        route_outcomes = (
            ("groq", groq_first, ProviderError("one", kind="transient")),
            ("groq", groq_second, ProviderError("two", kind="transient")),
            ("gemini", gemini_model, ProviderError("three", kind="transient")),
            ("cloudflare", cloudflare_model, "fourth model response"),
        )
        providers = {
            model_route_id(provider_id, model): FakeProvider(provider_id, outcome)
            for provider_id, model, outcome in route_outcomes
        }
        statuses = [
            provider_status(provider_id, model=model)
            for provider_id, model, _outcome in route_outcomes
        ]
        engine = self.make_engine(providers, statuses)
        engine.store.save_router_settings(
            RouterSettings(
                enabled_provider_ids=("groq", "gemini", "cloudflare"),
                priorities={"groq": 10, "gemini": 9, "cloudflare": 8},
            )
        )
        task = engine.create_task("Try four model routes")
        plan = engine.route_plan(task.instruction)
        self.assertEqual(
            [candidate.route_id for candidate in plan],
            [model_route_id(*entry[:2]) for entry in route_outcomes],
        )
        engine.approve_task(
            task.id,
            provider_ids=[candidate.route_id for candidate in plan],
            external_consent=True,
        )

        engine.run_next_task()

        stored = engine.store.get(task.id)
        self.assertEqual(stored.status, "completed")
        self.assertEqual(stored.result, "fourth model response")
        self.assertEqual(stored.selected_model, cloudflare_model)
        self.assertEqual(
            [len(providers[model_route_id(*entry[:2])].calls) for entry in route_outcomes],
            [1, 1, 1, 1],
        )
        attempts = engine.store.attempt_history()
        self.assertEqual(len(attempts), 4)
        self.assertEqual(
            [attempt["model"] for attempt in reversed(attempts)],
            [entry[1] for entry in route_outcomes],
        )

    def test_groq_cloudflare_1010_blocks_groq_routes_and_falls_back_to_approved_route(self):
        first_model = "openai/gpt-oss-20b"
        blocked_sibling_model = "openai/gpt-oss-120b"
        backup_model = "gemini-3.8-flash"
        groq = GroqProvider("secret", first_model)
        blocked_sibling = FakeProvider("groq", "must not be attempted")
        backup = FakeProvider("gemini", "fallback after access block")
        providers = {
            model_route_id("groq", first_model): groq,
            model_route_id("groq", blocked_sibling_model): blocked_sibling,
            model_route_id("gemini", backup_model): backup,
        }
        statuses = [
            provider_status("groq", model=first_model),
            provider_status("groq", model=blocked_sibling_model),
            provider_status("gemini", model=backup_model),
        ]
        engine = self.make_engine(providers, statuses)
        engine.store.save_router_settings(
            RouterSettings(
                enabled_provider_ids=("groq", "gemini"),
                priorities={"groq": 10, "gemini": 9},
            )
        )
        task = engine.create_task("Use another approved provider after access block")
        plan = engine.route_plan(task.instruction)
        engine.approve_task(
            task.id,
            provider_ids=[candidate.route_id for candidate in plan],
            external_consent=True,
        )
        access_error = HTTPError(
            "https://api.groq.com/openai/v1/chat/completions",
            403,
            "Forbidden",
            {},
            BytesIO(b"<html>Cloudflare error 1010</html>"),
        )
        with patch("aibridge.providers.urlopen", side_effect=access_error):
            engine.run_next_task()

        stored = engine.store.get(task.id)
        self.assertEqual(stored.status, "completed")
        self.assertEqual(stored.provider_id, "gemini")
        self.assertEqual(stored.result, "fallback after access block")
        self.assertEqual(blocked_sibling.calls, [])
        self.assertEqual(backup.calls, [task.instruction])
        failed_attempt = next(
            attempt
            for attempt in engine.store.attempt_history()
            if attempt["outcome"] == "failed"
        )
        self.assertEqual(failed_attempt["provider_id"], "groq")
        self.assertEqual(failed_attempt["model"], first_model)
        self.assertIn("access_blocked", failed_attempt["reason"])
        groq_status = next(
            provider for provider in engine.public_status()["providers"]
            if provider["id"] == "groq"
        )
        self.assertFalse(groq_status["enabled"])

    def test_model_route_fallback_has_a_fixed_bound(self):
        self.assertEqual(MAX_FALLBACK_ROUTES, 8)
        models = [f"verified-test-model-{index}" for index in range(10)]
        statuses = [
            provider_status("one", model=model)
            for model in models
        ]
        candidates = rank_providers(
            "Try a bounded set",
            statuses,
            RouterSettings(enabled_provider_ids=("one",)),
            {},
            now=0,
        )
        self.assertEqual(len(candidates), MAX_FALLBACK_ROUTES)
        self.assertEqual(len({candidate.route_id for candidate in candidates}), 8)

    def test_credentials_error_disables_provider_and_does_not_fallback(self):
        invalid_key = FakeProvider(
            "first",
            ProviderError(
                "api-key-sentinel unauthorized", kind="credentials", http_status=401
            ),
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
        self.assertNotIn("api-key-sentinel", engine.store.get(task.id).error)
        self.assertIn(
            "second::second-test-model: no intentado por la política de credenciales",
            engine.store.get(task.id).error,
        )
        self.assertNotIn("api-key-sentinel", state["attempts"][0]["reason"])
        self.assertNotIn("api-key-sentinel", state["health"][0]["last_error"])
        self.assertEqual(second.calls, [])

    def test_paid_provider_is_blocked_even_if_a_stale_task_authorizes_it(self):
        paid = FakeProvider("paid", "must not run")
        paid_status = ProviderStatus(
            **{
                **provider_status("paid").__dict__,
                "free_only": False,
            }
        )
        engine = self.make_engine({"paid": paid}, [paid_status])
        task = engine.create_task("Do not send to a paid route")
        engine.store.approve(task.id, ["paid"], external_consent=True)
        engine.run_next_task()

        stored = engine.store.get(task.id)
        self.assertEqual(stored.status, "failed")
        self.assertIn("bloqueado por FREE_ONLY", stored.error)
        self.assertEqual(paid.calls, [])

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
            set(config.providers),
            {
                model_route_id("gemini", "gemini-3.8-flash"),
                model_route_id("groq", "openai/gpt-oss-20b"),
                model_route_id("openrouter", "openrouter/free"),
            },
        )
        statuses = {status.identifier: status for status in config.statuses}
        self.assertFalse(statuses["cloudflare"].enabled)
        self.assertIn("8000", statuses["cloudflare"].message)
        self.assertEqual(statuses["gemini"].context_window_tokens, 1_048_576)
        self.assertEqual(statuses["groq"].context_window_tokens, 131_072)
        self.assertIn("reasoning", statuses["gemini"].capabilities)
        self.assertNotIn("gemini-key", json.dumps(config.statuses, default=lambda item: item.__dict__))

    def test_free_model_registry_allows_multiple_registered_models_per_provider(self):
        config = configure_free_providers(
            {
                "GROQ_API_KEY": "groq-key",
                "AI_BRIDGE_GROQ_FREE_TIER_CONFIRMED": "true",
                "AI_BRIDGE_GROQ_MODELS": (
                    "openai/gpt-oss-20b,openai/gpt-oss-120b,provider/unverified"
                ),
            }
        )
        self.assertEqual(
            set(config.providers),
            {
                model_route_id("groq", "openai/gpt-oss-20b"),
                model_route_id("groq", "openai/gpt-oss-120b"),
            },
        )
        groq_statuses = [
            status for status in config.statuses if status.identifier == "groq"
        ]
        self.assertEqual(len(groq_statuses), 3)
        self.assertEqual(
            sum(status.enabled for status in groq_statuses),
            2,
        )
        unverified = next(status for status in groq_statuses if not status.free_only)
        self.assertFalse(unverified.enabled)
        self.assertIsNone(unverified.model)

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
        cases = (
            (429, "rate_limited"),
            (503, "transient"),
            (401, "credentials"),
            (403, "access_blocked"),
            (400, "invalid_request"),
        )
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

    def test_provider_request_timeout_is_bounded_by_remaining_task_time(self):
        response_body = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "gemini"}]}}]}
        ).encode()
        with patch(
            "aibridge.providers.urlopen", return_value=BytesIO(response_body)
        ) as mocked_urlopen:
            with provider_request_timeout(2.5):
                GeminiProvider("secret", "gemini-3.8-flash").generate("hello")
        self.assertEqual(mocked_urlopen.call_args.kwargs["timeout"], 2.5)

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
                if provider.identifier == "openrouter":
                    self.assertEqual(provider.selected_model, "openrouter/free")
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
        self.assertEqual(
            plan["candidates"][0]["route_id"],
            model_route_id("gemini", "gemini-3.8-flash"),
        )
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
        self.assertEqual(
            approved["allowed_provider_ids"],
            [model_route_id("gemini", "gemini-3.8-flash")],
        )
        for _ in range(50):
            if self.engine.store.get(task["id"]).status == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(self.engine.store.get(task["id"]).status, "completed")


if __name__ == "__main__":
    unittest.main()
