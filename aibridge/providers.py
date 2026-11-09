"""Official, opt-in text-generation provider adapters."""

import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MAX_PROVIDER_RESPONSE_BYTES = 1_000_000
PROVIDER_TIMEOUT_SECONDS = 15
FREE_ONLY = True
MAX_PROVIDER_OUTPUT_TOKENS = 512
MAX_CLOUDFLARE_LOCAL_NEURON_BUDGET = 8_000
_REQUEST_TIMEOUT_SECONDS: ContextVar[float | None] = ContextVar(
    "aibridge_provider_request_timeout_seconds", default=None
)


@contextmanager
def provider_request_timeout(timeout_seconds: float) -> Iterator[None]:
    token = _REQUEST_TIMEOUT_SECONDS.set(timeout_seconds)
    try:
        yield
    finally:
        _REQUEST_TIMEOUT_SECONDS.reset(token)


class ProviderError(RuntimeError):
    """Raised when an external provider cannot return a usable response."""

    def __init__(
        self, message: str, *, kind: str = "permanent", http_status: int | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status

    @property
    def retryable(self) -> bool:
        return self.kind in {
            "access_blocked",
            "invalid_response",
            "rate_limited",
            "transient",
            "model_unavailable",
        }


class AgentProvider(Protocol):
    """Minimal interface implemented by text-generation providers."""

    identifier: str
    display_name: str

    def generate(self, instruction: str) -> str: ...


@dataclass(frozen=True)
class ProviderStatus:
    identifier: str
    display_name: str
    configured: bool
    enabled: bool
    simulation: bool
    message: str
    model: str | None = None
    availability: str = "unknown"
    quota: str = "No se consultó la cuota del proveedor."
    free_only: bool = True
    capabilities: tuple[str, ...] = ("text",)
    context_window_tokens: int | None = None
    max_output_tokens: int = MAX_PROVIDER_OUTPUT_TOKENS
    local_daily_request_limit: int = 20
    local_daily_token_limit: int = 20_000

    @property
    def route_id(self) -> str:
        return model_route_id(self.identifier, self.model) if self.model else self.identifier


@dataclass(frozen=True)
class FreeModel:
    provider_id: str
    model: str
    capabilities: tuple[str, ...]
    context_window_tokens: int | None
    max_output_tokens: int


def model_route_id(provider_id: str, model: str) -> str:
    return f"{provider_id}::{model}"


@dataclass(frozen=True)
class ProviderConfiguration:
    statuses: tuple[ProviderStatus, ...]
    selected_provider: AgentProvider | None
    selected_status: ProviderStatus | None


@dataclass(frozen=True)
class FreeProviderConfiguration:
    providers: dict[str, AgentProvider]
    statuses: tuple[ProviderStatus, ...]
    sensitive_values: tuple[str, ...]
    free_only: bool = True


def _post_json(url: str, headers: dict[str, str], payload: dict[str, object]) -> object:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        request_timeout = _REQUEST_TIMEOUT_SECONDS.get()
        timeout = (
            min(PROVIDER_TIMEOUT_SECONDS, max(0.001, request_timeout))
            if request_timeout is not None
            else PROVIDER_TIMEOUT_SECONDS
        )
        with urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except HTTPError as error:
        try:
            error_body = error.read(MAX_PROVIDER_RESPONSE_BYTES + 1).decode(
                "utf-8", errors="ignore"
            ).lower()
        except OSError:
            error_body = ""
        invalid_key = any(
            marker in error_body
            for marker in ("api_key_invalid", "invalid api key", "invalid_api_key")
        )
        unavailable_model = any(
            marker in error_body
            for marker in (
                "model not found",
                "not_found",
                "model is not supported",
                "unknown model",
            )
        )
        cloudflare_access_block = (
            error.code == 403
            and "1010" in error_body
            and "cloudflare" in error_body
        )
        if cloudflare_access_block:
            kind = "access_blocked"
        elif error.code == 429:
            kind = "rate_limited"
        elif error.code in {408, 500, 502, 503, 504}:
            kind = "transient"
        elif error.code == 401 or invalid_key:
            kind = "credentials"
        elif error.code == 403:
            kind = "access_blocked"
        elif error.code == 404:
            kind = "model_unavailable"
        elif unavailable_model:
            kind = "model_unavailable"
        elif error.code in {400, 422}:
            kind = "invalid_request"
        else:
            kind = "permanent"
        message = (
            "Bloqueo de acceso Cloudflare 1010 (HTTP 403); no se intentará eludir la protección."
            if cloudflare_access_block
            else f"El proveedor rechazó la solicitud (HTTP {error.code})."
        )
        raise ProviderError(
            message,
            kind=kind,
            http_status=error.code,
        ) from error
    except URLError as error:
        reason = error.reason
        message = (
            "Se agotó el tiempo de espera del proveedor."
            if isinstance(reason, TimeoutError)
            else "No se pudo conectar con el proveedor."
        )
        raise ProviderError(message, kind="transient") from error
    except TimeoutError as error:
        raise ProviderError(
            "Se agotó el tiempo de espera del proveedor.", kind="transient"
        ) from error
    except OSError as error:
        raise ProviderError(
            "No se pudo completar la conexión con el proveedor.", kind="transient"
        ) from error

    if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ProviderError(
            "La respuesta del proveedor supera el límite permitido.",
            kind="invalid_response",
        )
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError(
            "El proveedor devolvió una respuesta no válida.", kind="invalid_response"
        ) from error


def _response_text(response: object, provider_name: str) -> str:
    if not isinstance(response, dict):
        raise ProviderError(f"{provider_name} devolvió una respuesta no válida.")
    text = response.get("output_text")
    if isinstance(text, str) and text.strip():
        return text

    output = response.get("output")
    if isinstance(output, list):
        parts = []
        for block in output:
            if not isinstance(block, dict) or not isinstance(block.get("content"), list):
                continue
            parts.extend(
                item.get("text")
                for item in block["content"]
                if isinstance(item, dict) and item.get("type") == "output_text"
            )
        if parts and all(isinstance(part, str) for part in parts):
            combined = "".join(parts)
            if combined.strip():
                return combined
    raise ProviderError(f"{provider_name} no devolvió texto de respuesta.")


class OpenAICodexProvider:
    """Use the official OpenAI Responses API with an account-enabled Codex model."""

    identifier = "openai_codex"
    display_name = "OpenAI Codex (Responses API)"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def generate(self, instruction: str) -> str:
        response = _post_json(
            "https://api.openai.com/v1/responses",
            {"Authorization": f"Bearer {self._api_key}"},
            {"model": self._model, "input": instruction, "store": False},
        )
        return _response_text(response, self.display_name)


class DeepSeekProvider:
    """Use DeepSeek's official chat-completions API."""

    identifier = "deepseek"
    display_name = "DeepSeek"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def generate(self, instruction: str) -> str:
        response = _post_json(
            "https://api.deepseek.com/chat/completions",
            {"Authorization": f"Bearer {self._api_key}"},
            {
                "model": self._model,
                "messages": [{"role": "user", "content": instruction}],
                "stream": False,
            },
        )
        if not isinstance(response, dict):
            raise ProviderError("DeepSeek devolvió una respuesta no válida.")
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("DeepSeek no devolvió texto de respuesta.")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("DeepSeek no devolvió texto de respuesta.")
        return content


class ClaudeProvider:
    """Use Anthropic's official Messages API."""

    identifier = "claude"
    display_name = "Claude"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def generate(self, instruction: str) -> str:
        response = _post_json(
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
            {
                "model": self._model,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": instruction}],
            },
        )
        if not isinstance(response, dict):
            raise ProviderError("Claude devolvió una respuesta no válida.")
        content = response.get("content")
        if not isinstance(content, list):
            raise ProviderError("Claude no devolvió texto de respuesta.")
        parts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not parts or not all(isinstance(part, str) for part in parts):
            raise ProviderError("Claude no devolvió texto de respuesta.")
        result = "".join(parts)
        if not result.strip():
            raise ProviderError("Claude no devolvió texto de respuesta.")
        return result


class GeminiProvider:
    """Use Google's Gemini Developer API with a free-tier model only."""

    identifier = "gemini"
    display_name = "Google Gemini API"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def generate(self, instruction: str) -> str:
        if not re.fullmatch(r"gemini-[a-zA-Z0-9.-]+", self._model):
            raise ProviderError(
                "El identificador del modelo Gemini no es válido.",
                kind="invalid_request",
            )
        response = _post_json(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._model}:generateContent",
            {"x-goog-api-key": self._api_key},
            {
                "contents": [{"parts": [{"text": instruction}]}],
                "generationConfig": {"maxOutputTokens": MAX_PROVIDER_OUTPUT_TOKENS},
            },
        )
        if not isinstance(response, dict):
            raise ProviderError("Gemini devolvió una respuesta no válida.", kind="invalid_response")
        prompt_feedback = response.get("promptFeedback")
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            raise ProviderError(
                "Gemini rechazó la solicitud por sus políticas de seguridad.",
                kind="safety_rejection",
            )
        candidates = response.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else None
        if not isinstance(candidate, dict):
            raise ProviderError("Gemini no devolvió texto de respuesta.", kind="invalid_response")
        if candidate.get("finishReason") == "SAFETY":
            raise ProviderError(
                "Gemini rechazó la solicitud por sus políticas de seguridad.",
                kind="safety_rejection",
            )
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        text = "".join(
            part["text"]
            for part in parts or []
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
        if not text.strip():
            raise ProviderError("Gemini no devolvió texto de respuesta.", kind="invalid_response")
        return text


class OpenAICompatibleProvider:
    """Call a fixed OpenAI-compatible endpoint for text-only chat completions."""

    def __init__(
        self,
        identifier: str,
        display_name: str,
        endpoint: str,
        api_key: str,
        model: str,
    ) -> None:
        self.identifier = identifier
        self.display_name = display_name
        self._endpoint = endpoint
        self._api_key = api_key
        self._model = model
        self.selected_model = model

    def generate(self, instruction: str) -> str:
        response = _post_json(
            self._endpoint,
            {"Authorization": f"Bearer {self._api_key}"},
            {
                "model": self._model,
                "messages": [{"role": "user", "content": instruction}],
                "max_tokens": MAX_PROVIDER_OUTPUT_TOKENS,
                "stream": False,
            },
        )
        if not isinstance(response, dict):
            raise ProviderError(
                f"{self.display_name} devolvió una respuesta no válida.",
                kind="invalid_response",
            )
        choices = response.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        if not isinstance(choice, dict):
            raise ProviderError(
                f"{self.display_name} no devolvió texto de respuesta.",
                kind="invalid_response",
            )
        if choice.get("finish_reason") == "content_filter":
            raise ProviderError(
                f"{self.display_name} rechazó la solicitud por sus políticas de seguridad.",
                kind="safety_rejection",
            )
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError(
                f"{self.display_name} no devolvió texto de respuesta.",
                kind="invalid_response",
            )
        return content


class GroqProvider(OpenAICompatibleProvider):
    """Use Groq's official OpenAI-compatible API."""

    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(
            "groq",
            "GroqCloud",
            "https://api.groq.com/openai/v1/chat/completions",
            api_key,
            model,
        )


class OpenRouterProvider(OpenAICompatibleProvider):
    """Use OpenRouter's official API with a free-only model identifier."""

    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(
            "openrouter",
            "OpenRouter",
            "https://openrouter.ai/api/v1/chat/completions",
            api_key,
            model,
        )


class CloudflareWorkersAIProvider:
    """Use the official Workers AI REST API."""

    identifier = "cloudflare"
    display_name = "Cloudflare Workers AI"

    def __init__(self, api_token: str, account_id: str, model: str) -> None:
        self._api_token = api_token
        self._account_id = account_id
        self._model = model

    def generate(self, instruction: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", self._account_id):
            raise ProviderError(
                "El identificador de cuenta de Cloudflare no es válido.",
                kind="invalid_request",
            )
        response = _post_json(
            f"https://api.cloudflare.com/client/v4/accounts/{self._account_id}"
            f"/ai/run/{self._model}",
            {"Authorization": f"Bearer {self._api_token}"},
            {
                "messages": [{"role": "user", "content": instruction}],
                "max_tokens": 256,
            },
        )
        result = response.get("result") if isinstance(response, dict) else None
        text = result.get("response") if isinstance(result, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ProviderError(
                "Cloudflare Workers AI no devolvió texto de respuesta.",
                kind="invalid_response",
            )
        return text


_PROVIDER_DEFINITIONS = (
    (
        "openai_codex",
        OpenAICodexProvider.display_name,
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        OpenAICodexProvider,
    ),
    (
        "deepseek",
        DeepSeekProvider.display_name,
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        DeepSeekProvider,
    ),
    (
        "claude",
        ClaudeProvider.display_name,
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        ClaudeProvider,
    ),
)

_FREE_MODEL_ALLOWLISTS = {
    "gemini": {"gemini-3.8-flash"},
    "groq": {"openai/gpt-oss-20b", "openai/gpt-oss-120b"},
    "openrouter": {"openrouter/free"},
    "cloudflare": {"@cf/meta/llama-3.2-1b-instruct"},
}
_FREE_ROUTER_MODEL = "openrouter/free"
FREE_MODEL_REGISTRY = {
    model.provider_id + "::" + model.model: model
    for model in (
        FreeModel(
            "gemini",
            "gemini-3.8-flash",
            ("text", "programming", "reasoning"),
            1_048_576,
            MAX_PROVIDER_OUTPUT_TOKENS,
        ),
        FreeModel(
            "groq",
            "openai/gpt-oss-20b",
            ("text", "programming", "reasoning"),
            131_072,
            MAX_PROVIDER_OUTPUT_TOKENS,
        ),
        FreeModel(
            "groq",
            "openai/gpt-oss-120b",
            ("text", "programming", "reasoning"),
            131_072,
            MAX_PROVIDER_OUTPUT_TOKENS,
        ),
        FreeModel("openrouter", "openrouter/free", ("text",), None, MAX_PROVIDER_OUTPUT_TOKENS),
        FreeModel(
            "cloudflare",
            "@cf/meta/llama-3.2-1b-instruct",
            ("text",),
            None,
            256,
        ),
    )
}
_LOCAL_DAILY_REQUEST_LIMIT = 20
_LOCAL_DAILY_TOKEN_LIMIT = 20_000


def _strict_true(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() == "true"


def configure_free_providers(
    environment: Mapping[str, str] | None = None,
) -> FreeProviderConfiguration:
    """Configure only explicitly confirmed, registered free model routes."""
    env = os.environ if environment is None else environment
    providers: dict[str, AgentProvider] = {}
    statuses: list[ProviderStatus] = []
    definitions = (
        (
            "gemini",
            GeminiProvider.display_name,
            "GEMINI_API_KEY",
            "AI_BRIDGE_GEMINI_MODEL",
            "AI_BRIDGE_GEMINI_MODELS",
            "gemini-3.8-flash",
            GeminiProvider,
            "AI_BRIDGE_GEMINI_FREE_TIER_CONFIRMED",
            "Confirma en el entorno del servidor que el proyecto usa el nivel gratuito.",
        ),
        (
            "groq",
            "GroqCloud",
            "GROQ_API_KEY",
            "AI_BRIDGE_GROQ_MODEL",
            "AI_BRIDGE_GROQ_MODELS",
            "openai/gpt-oss-20b,openai/gpt-oss-120b",
            GroqProvider,
            "AI_BRIDGE_GROQ_FREE_TIER_CONFIRMED",
            "Confirma en el entorno del servidor que la cuenta mantiene el plan gratuito.",
        ),
        (
            "openrouter",
            "OpenRouter",
            "OPENROUTER_API_KEY",
            "AI_BRIDGE_OPENROUTER_MODEL",
            "AI_BRIDGE_OPENROUTER_MODELS",
            _FREE_ROUTER_MODEL,
            OpenRouterProvider,
            "",
            "",
        ),
        (
            "cloudflare",
            CloudflareWorkersAIProvider.display_name,
            "CLOUDFLARE_API_TOKEN",
            "AI_BRIDGE_CLOUDFLARE_MODEL",
            "AI_BRIDGE_CLOUDFLARE_MODELS",
            "@cf/meta/llama-3.2-1b-instruct",
            None,
            "AI_BRIDGE_CLOUDFLARE_FREE_PLAN_CONFIRMED",
            "Confirma el plan Workers Free y configura un presupuesto diario dedicado.",
        ),
    )
    for (
        identifier,
        display_name,
        key_name,
        model_name,
        models_name,
        default_model,
        provider_type,
        confirmation_name,
        confirmation_message,
    ) in definitions:
        key = env.get(key_name, "").strip()
        configured_models = env.get(models_name, "").strip()
        model_value = configured_models or env.get(model_name, default_model)
        models = tuple(
            dict.fromkeys(
                item.strip() for item in model_value.split(",") if item.strip()
            )
        )
        account_id = env.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        configured = bool(key)
        if identifier == "cloudflare":
            configured = bool(key and account_id)
        allowlist = _FREE_MODEL_ALLOWLISTS.get(identifier)
        confirmation_ok = (
            not confirmation_name or _strict_true(env, confirmation_name)
        )
        budget_ok = True
        if identifier == "cloudflare":
            try:
                budget = int(env.get("AI_BRIDGE_CLOUDFLARE_DAILY_NEURON_BUDGET", "0"))
            except ValueError:
                budget = 0
            budget_ok = 1 <= budget <= MAX_CLOUDFLARE_LOCAL_NEURON_BUDGET

        provider_enabled = configured and confirmation_ok and budget_ok
        if not configured:
            message = (
                f"Falta {key_name}"
                + (" o CLOUDFLARE_ACCOUNT_ID." if identifier == "cloudflare" else ".")
            )
        elif not confirmation_ok:
            message = f"Deshabilitado: {confirmation_message}"
        elif not budget_ok:
            message = (
                "Deshabilitado: fija AI_BRIDGE_CLOUDFLARE_DAILY_NEURON_BUDGET "
                f"entre 1 y {MAX_CLOUDFLARE_LOCAL_NEURON_BUDGET}."
            )
        else:
            message = "Configurado para uso gratuito; requiere aprobación por tarea."

        quota_message = (
            "Cuota oficial no consultada. Límite local: "
            f"{_LOCAL_DAILY_REQUEST_LIMIT} solicitudes y "
            f"{_LOCAL_DAILY_TOKEN_LIMIT} tokens estimados al día."
        )
        if identifier == "cloudflare":
            quota_message = (
                "Plan Workers Free confirmado por el operador; presupuesto local "
                f"configurado: {budget if provider_enabled else 0} neuronas/día (máximo "
                f"{MAX_CLOUDFLARE_LOCAL_NEURON_BUDGET}). La cuota compartida no se consulta."
            )
        for model in models:
            model_spec = FREE_MODEL_REGISTRY.get(model_route_id(identifier, model))
            model_allowed = model_spec is not None and (
                allowlist is None or model in allowlist
            )
            enabled = provider_enabled and model_allowed
            if not model_allowed:
                model_message = (
                    "Deshabilitado: el modelo no está en el registro gratuito verificado."
                )
            else:
                model_message = message
            if enabled:
                if identifier == "cloudflare":
                    provider: AgentProvider = CloudflareWorkersAIProvider(key, account_id, model)
                elif provider_type is not None:
                    provider = provider_type(key, model)
                else:
                    continue
                providers[model_route_id(identifier, model)] = provider
            statuses.append(
                ProviderStatus(
                    identifier=identifier,
                    display_name=display_name,
                    configured=configured,
                    enabled=enabled,
                    simulation=False,
                    message=model_message,
                    model=model if model_allowed else None,
                    availability="not_checked" if enabled else "disabled",
                    quota=quota_message
                    if enabled
                    else "Cuota gratuita no verificada; no se harán solicitudes.",
                    free_only=FREE_ONLY if model_allowed else False,
                    capabilities=model_spec.capabilities if model_spec else ("text",),
                    context_window_tokens=(
                        model_spec.context_window_tokens if model_spec else None
                    ),
                    max_output_tokens=(
                        model_spec.max_output_tokens
                        if model_spec
                        else MAX_PROVIDER_OUTPUT_TOKENS
                    ),
                )
            )
        if not models:
            statuses.append(
                ProviderStatus(
                    identifier=identifier,
                    display_name=display_name,
                    configured=configured,
                    enabled=False,
                    simulation=False,
                    message="No hay modelos configurados.",
                    free_only=False,
                )
            )
    return FreeProviderConfiguration(
        providers=providers,
        statuses=tuple(statuses),
        sensitive_values=configured_api_keys(env),
    )


def configured_api_keys(environment: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return configured credential values for local redaction, never for serialization."""
    env = os.environ if environment is None else environment
    key_names = {definition[2] for definition in _PROVIDER_DEFINITIONS}
    key_names.update(
        {
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "OPENROUTER_API_KEY",
            "CLOUDFLARE_API_TOKEN",
        }
    )
    return tuple(
        sorted(
            {
                env[name].strip()
                for name in key_names
                if env.get(name, "").strip()
            },
            key=len,
            reverse=True,
        )
    )


def configure_providers(
    environment: Mapping[str, str] | None = None,
) -> ProviderConfiguration:
    """Build only the explicitly selected, configured, and consented provider."""
    env = os.environ if environment is None else environment
    selected_identifier = env.get("AI_BRIDGE_PROVIDER", "").strip().lower()
    consented = env.get("AI_BRIDGE_ALLOW_PAID_API", "").strip().lower() == "true"
    statuses = []
    selected_provider = None
    selected_status = None

    known_identifiers = {definition[0] for definition in _PROVIDER_DEFINITIONS}
    for identifier, display_name, key_name, model_name, provider_type in _PROVIDER_DEFINITIONS:
        configured = bool(env.get(key_name, "").strip() and env.get(model_name, "").strip())
        is_selected = identifier == selected_identifier
        enabled = configured and is_selected and consented
        if not configured:
            message = f"Configura {key_name} y {model_name} en el entorno del servidor."
        elif not is_selected:
            message = "Proveedor configurado, pero no seleccionado."
        elif not consented:
            message = "Deshabilitado: requiere AI_BRIDGE_ALLOW_PAID_API=true."
        else:
            message = "Configurado y habilitado; las solicitudes requieren aprobación."
        status = ProviderStatus(
            identifier=identifier,
            display_name=display_name,
            configured=configured,
            enabled=enabled,
            simulation=False,
            message=message,
            free_only=False,
        )
        statuses.append(status)
        if is_selected:
            selected_status = status
            if enabled:
                selected_provider = provider_type(
                    env[key_name].strip(), env[model_name].strip()
                )

    if selected_identifier and selected_identifier not in known_identifiers:
        selected_status = ProviderStatus(
            identifier=selected_identifier,
            display_name=selected_identifier,
            configured=False,
            enabled=False,
            simulation=False,
            message="Proveedor desconocido. Elige openai_codex, deepseek o claude.",
            free_only=False,
        )

    return ProviderConfiguration(
        statuses=tuple(statuses),
        selected_provider=selected_provider,
        selected_status=selected_status,
    )
