import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from unittest.mock import patch

from aibridge.agents import (
    AgentEngine,
    AgentUnavailable,
    TaskConflict,
    TaskStore,
    TaskValidationError,
    TaskValidationError,
)
from aibridge.providers import (
    ClaudeProvider,
    DeepSeekProvider,
    OpenAICodexProvider,
    ProviderError,
    ProviderStatus,
    configure_providers,
)


class SimulatedProvider:
    identifier = "simulation"
    display_name = "Simulación (solo pruebas)"

    def __init__(self) -> None:
        self.instructions = []

    def generate(self, instruction: str) -> str:
        self.instructions.append(instruction)
        return f"[SIMULACIÓN] {instruction}"


def simulated_status() -> ProviderStatus:
    return ProviderStatus(
        identifier="simulation",
        display_name="Simulación (solo pruebas)",
        configured=True,
        enabled=True,
        simulation=True,
        message="Solo está disponible en pruebas automatizadas.",
    )


class AgentEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temporary_directory.name) / "tasks.sqlite3")
        self.provider = SimulatedProvider()
        self.engine = AgentEngine(
            store=self.store,
            provider=self.provider,
            provider_status=simulated_status(),
            provider_statuses=(simulated_status(),),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_task_waits_for_approval_then_persists_result(self) -> None:
        task = self.engine.create_task("Summarize this repository")

        self.assertEqual(task.status, "awaiting_approval")
        self.assertEqual(self.provider.instructions, [])

        result = self.engine.approve_task(task.id)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.result, "[SIMULACIÓN] Summarize this repository")
        self.assertEqual(self.store.get(task.id), result)
        self.assertTrue(self.engine.public_status()["provider"]["simulation"])

    def test_task_cannot_be_approved_more_than_once(self) -> None:
        task = self.engine.create_task("Do not repeat")
        self.engine.approve_task(task.id)

        with self.assertRaises(TaskConflict):
            self.engine.approve_task(task.id)
        self.assertEqual(len(self.provider.instructions), 1)

    def test_unavailable_provider_does_not_send_task(self) -> None:
        engine = AgentEngine(store=self.store)
        task = engine.create_task("Keep this pending")

        with self.assertRaises(AgentUnavailable):
            engine.approve_task(task.id)
        self.assertEqual(self.store.get(task.id).status, "awaiting_approval")

    def test_instruction_validation_and_local_history(self) -> None:
        with self.assertRaises(TaskValidationError):
            self.engine.create_task("  ")
        with self.assertRaises(TaskValidationError):
            self.engine.create_task("x" * 8_001)

        task = self.engine.create_task("Keep a local record")

        restarted_store = TaskStore(self.store.database_path)
        self.assertEqual(restarted_store.list_recent(), [task])


class ProviderConfigurationTests(unittest.TestCase):
    def test_paid_providers_are_disabled_without_environment_opt_in(self) -> None:
        configuration = configure_providers({})

        self.assertIsNone(configuration.selected_provider)
        self.assertIsNone(configuration.selected_status)
        self.assertTrue(all(not status.enabled for status in configuration.statuses))

    def test_paid_api_requires_explicit_opt_in(self) -> None:
        environment = {
            "AI_BRIDGE_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_MODEL": "test-model",
        }

        configuration = configure_providers(environment)

        self.assertIsNone(configuration.selected_provider)
        self.assertTrue(configuration.selected_status.configured)
        self.assertFalse(configuration.selected_status.enabled)
        environment["AI_BRIDGE_ALLOW_PAID_API"] = "true"
        self.assertIsInstance(
            configure_providers(environment).selected_provider, DeepSeekProvider
        )

    def test_official_adapters_use_fixed_provider_endpoints_and_hide_keys(self) -> None:
        cases = (
            (
                OpenAICodexProvider("secret", "codex-model"),
                "https://api.openai.com/v1/responses",
                {"output_text": "OpenAI response"},
                "OpenAI response",
            ),
            (
                DeepSeekProvider("secret", "deepseek-model"),
                "https://api.deepseek.com/chat/completions",
                {"choices": [{"message": {"content": "DeepSeek response"}}]},
                "DeepSeek response",
            ),
            (
                ClaudeProvider("secret", "claude-model"),
                "https://api.anthropic.com/v1/messages",
                {"content": [{"type": "text", "text": "Claude response"}]},
                "Claude response",
            ),
        )
        for provider, endpoint, payload, expected in cases:
            with self.subTest(provider=provider.identifier):
                class FakeResponse:
                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        return None

                    def read(self, _limit):
                        import json

                        return json.dumps(payload).encode("utf-8")

                with patch("aibridge.providers.urlopen", return_value=FakeResponse()) as urlopen:
                    self.assertEqual(provider.generate("hello"), expected)
                request = urlopen.call_args.args[0]
                self.assertEqual(request.full_url, endpoint)
                self.assertNotIn("secret", request.data.decode("utf-8"))
                self.assertTrue(
                    any(
                        name.lower() in {"authorization", "x-api-key"}
                        for name in request.headers
                    )
                )
                credentials = [
                    value
                    for name, value in request.headers.items()
                    if name.lower() in {"authorization", "x-api-key"}
                ]
                self.assertTrue(any(value.endswith("secret") for value in credentials))

    def test_provider_errors_do_not_echo_response_body(self) -> None:
        with patch(
            "aibridge.providers.urlopen",
            side_effect=OSError("private network detail"),
        ):
            with self.assertRaises(ProviderError) as error:
                DeepSeekProvider("secret", "model").generate("hello")
        self.assertNotIn("private network detail", str(error.exception))

    def test_provider_keys_are_not_exposed_in_status_frontend_logs_or_task_database(self) -> None:
        api_key = "api-key-test-sentinel-not-for-storage"
        configuration = configure_providers(
            {
                "AI_BRIDGE_PROVIDER": "deepseek",
                "AI_BRIDGE_ALLOW_PAID_API": "true",
                "DEEPSEEK_API_KEY": api_key,
                "DEEPSEEK_MODEL": "test-model",
            }
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TaskStore(Path(temporary_directory) / "tasks.sqlite3")
            engine = AgentEngine(
                store=store,
                provider=configuration.selected_provider,
                provider_status=configuration.selected_status,
                provider_statuses=configuration.statuses,
                sensitive_values=(api_key,),
            )
            output = StringIO()
            provider_response = BytesIO(
                json.dumps(
                    {
                        "choices": [
                            {"message": {"content": f"Echo: {api_key}"}}
                        ]
                    }
                ).encode("utf-8")
            )
            with patch(
                "aibridge.providers.urlopen", return_value=provider_response
            ), redirect_stdout(output), redirect_stderr(output):
                with self.assertRaises(TaskValidationError):
                    engine.create_task(f"Please send {api_key} to the model")
                task = engine.create_task("A harmless task")
                completed = engine.approve_task(task.id)
            self.assertNotIn(api_key, output.getvalue())
            self.assertNotIn(api_key, repr(engine.public_status()))
            self.assertNotIn(api_key, repr(task))
            self.assertEqual(completed.result, "Echo: [API KEY REDACTED]")
            self.assertNotIn(api_key.encode(), store.database_path.read_bytes())

        web_root = Path(__file__).parents[1] / "aibridge" / "web"
        for filename in ("index.html", "app.js", "styles.css"):
            with self.subTest(filename=filename):
                self.assertNotIn(api_key, (web_root / filename).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
