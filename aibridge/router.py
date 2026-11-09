"""Free-only model routing with explainable preferences and health signals."""

from dataclasses import dataclass, field
import time
from typing import Mapping, Sequence

from aibridge.providers import ProviderStatus

MAX_FALLBACK_PROVIDERS = 3
MAX_FALLBACK_ROUTES = 8
MAX_TASK_SECONDS = 60
PROVIDER_COOLDOWN_SECONDS = 60
LOCAL_DAILY_REQUEST_LIMIT = 20
LOCAL_DAILY_TOKEN_LIMIT = 20_000
MAX_ESTIMATED_OUTPUT_TOKENS = 512

_DOCUMENTED_TASK_FIT = {
    ("gemini", "gemini-3.8-flash"): frozenset({"programming", "reasoning"}),
    ("groq", "openai/gpt-oss-120b"): frozenset({"programming", "reasoning"}),
    ("groq", "openai/gpt-oss-20b"): frozenset({"programming", "reasoning"}),
}


@dataclass(frozen=True)
class RouterSettings:
    mode: str = "automatic"
    manual_provider_id: str | None = None
    enabled_provider_ids: tuple[str, ...] = ()
    priorities: Mapping[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "manual_provider_id": self.manual_provider_id,
            "enabled_provider_ids": list(self.enabled_provider_ids),
            "priorities": dict(self.priorities),
        }

    @classmethod
    def from_value(cls, value: object) -> "RouterSettings":
        if not isinstance(value, dict):
            return cls()
        mode = value.get("mode")
        if mode not in {"automatic", "manual"}:
            mode = "automatic"
        manual_id = value.get("manual_provider_id")
        if not isinstance(manual_id, str):
            manual_id = None
        enabled = value.get("enabled_provider_ids")
        enabled_ids = (
            tuple(item for item in enabled if isinstance(item, str))
            if isinstance(enabled, list)
            else ()
        )
        raw_priorities = value.get("priorities")
        priorities: dict[str, int] = {}
        if isinstance(raw_priorities, dict):
            for provider_id, priority in raw_priorities.items():
                if isinstance(provider_id, str) and isinstance(priority, int):
                    priorities[provider_id] = max(-10, min(10, priority))
        return cls(mode, manual_id, enabled_ids, priorities)


@dataclass
class ProviderHealth:
    available_at: float = 0
    last_error: str | None = None
    last_failure_kind: str | None = None
    successes: int = 0
    failures: int = 0
    latency_ms: float | None = None


@dataclass(frozen=True)
class RouteCandidate:
    provider_id: str
    model: str
    score: float
    explanation: str

    @property
    def route_id(self) -> str:
        return f"{self.provider_id}::{self.model}"


def classify_task(instruction: str) -> str:
    """Return a heuristic label for display; it is not a quality judgement."""
    text = instruction.casefold()
    coding_terms = (
        "code",
        "coding",
        "program",
        "debug",
        "python",
        "javascript",
        "implement",
        "código",
        "programación",
        "depura",
        "implementar",
    )
    reasoning_terms = (
        "reason",
        "analy",
        "compare",
        "evaluate",
        "logic",
        "razon",
        "analiza",
        "compara",
        "evalúa",
    )
    if any(term in text for term in coding_terms):
        return "programming"
    if any(term in text for term in reasoning_terms):
        return "reasoning"
    return "general"


def rank_providers(
    instruction: str,
    statuses: Sequence[ProviderStatus],
    settings: RouterSettings,
    health: Mapping[str, ProviderHealth],
    *,
    now: float,
    usage: Mapping[str, tuple[int, int]] | None = None,
) -> list[RouteCandidate]:
    """Order eligible candidates without making unsupported quality claims."""
    task_type = classify_task(instruction)
    estimated_prompt_tokens = (len(instruction) + 2) // 3
    enabled_ids = set(settings.enabled_provider_ids)
    usage = usage or {}
    candidates: list[RouteCandidate] = []
    for status in statuses:
        if not status.enabled or not status.free_only or status.simulation:
            continue
        if status.identifier not in enabled_ids:
            continue
        if "text" not in status.capabilities:
            continue
        if (
            status.context_window_tokens is not None
            and estimated_prompt_tokens + MAX_ESTIMATED_OUTPUT_TOKENS
            > status.context_window_tokens
        ):
            continue
        state = health.get(status.route_id, health.get(status.identifier, ProviderHealth()))
        if state.available_at > now:
            continue
        requests, tokens = usage.get(status.identifier, (0, 0))
        if (
            requests >= LOCAL_DAILY_REQUEST_LIMIT
            or tokens + MAX_ESTIMATED_OUTPUT_TOKENS >= LOCAL_DAILY_TOKEN_LIMIT
        ):
            continue
        priority = settings.priorities.get(status.identifier, 0)
        task_fit = task_type in _DOCUMENTED_TASK_FIT.get(
            (status.identifier, status.model or ""), frozenset()
        )
        success_ratio = state.successes / max(1, state.successes + state.failures)
        latency_penalty = min((state.latency_ms or 0) / 10_000, 1)
        score = priority * 100 + (10 if task_fit else 0) + success_ratio * 5 - latency_penalty
        score_note = (
            "prioridad del usuario"
            if priority
            else "sin preferencia de usuario; orden por capacidad documentada y salud observada"
        )
        if task_fit:
            score_note += "; capacidad descrita por el proveedor, no medición comparativa"
        if state.latency_ms is not None:
            score_note += f", latencia observada {state.latency_ms:.0f} ms"
        if state.successes or state.failures:
            score_note += (
                f", {state.successes} éxitos de "
                f"{state.successes + state.failures} intentos"
            )
        candidates.append(
            RouteCandidate(
                status.identifier,
                status.model or "",
                score,
                f"Tarea {task_type}; {score_note}.",
            )
        )
    candidates.sort(
        key=lambda candidate: (
            0
            if settings.mode == "manual"
            and candidate.provider_id == settings.manual_provider_id
            else 1,
            -candidate.score,
            candidate.provider_id,
        )
    )
    routes: list[RouteCandidate] = []
    provider_ids: set[str] = set()
    route_ids: set[str] = set()
    for candidate in candidates:
        route_id = f"{candidate.provider_id}::{candidate.model}"
        if route_id in route_ids:
            continue
        if (
            candidate.provider_id not in provider_ids
            and len(provider_ids) >= MAX_FALLBACK_PROVIDERS
        ):
            continue
        routes.append(candidate)
        provider_ids.add(candidate.provider_id)
        route_ids.add(route_id)
        if len(routes) >= MAX_FALLBACK_ROUTES:
            break
    return routes


def fallback_reason(provider_id: str, error_kind: str) -> str:
    """Return a sanitized, user-facing explanation for a retry decision."""
    labels = {
        "access_blocked": "bloqueo de acceso; se suspende esta ruta",
        "invalid_response": "respuesta técnica no válida",
        "rate_limited": "límite temporal (HTTP 429)",
        "transient": "fallo temporal o timeout",
        "model_unavailable": "modelo no disponible",
    }
    return f"{provider_id}: {labels.get(error_kind, 'fallo temporal')}; se prueba el siguiente proveedor."


def public_health(health: Mapping[str, ProviderHealth]) -> list[dict[str, object]]:
    now = time.monotonic()
    return [
        {
            "route_id": provider_id,
            "provider_id": provider_id.split("::", 1)[0],
            "model": provider_id.split("::", 1)[1]
            if "::" in provider_id
            else None,
            "available": state.available_at <= now,
            "state": "cooldown" if state.available_at > now else "available",
            "retry_after_seconds": max(0, int(state.available_at - now)),
            "last_error": state.last_error,
            "last_failure_kind": state.last_failure_kind,
            "successes": state.successes,
            "failures": state.failures,
            "latency_ms": state.latency_ms,
        }
        for provider_id, state in health.items()
    ]
