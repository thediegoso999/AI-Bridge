"""Strictly free, asynchronous task routing and fallback orchestration."""

import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from aibridge.agents import (
    AgentUnavailable,
    TaskConflict,
    TaskNotFound,
    TaskValidationError,
    default_state_directory,
)
from aibridge.providers import (
    AgentProvider,
    ProviderError,
    ProviderStatus,
    configure_free_providers,
)
from aibridge.router import (
    LOCAL_DAILY_REQUEST_LIMIT,
    LOCAL_DAILY_TOKEN_LIMIT,
    MAX_ESTIMATED_OUTPUT_TOKENS,
    MAX_FALLBACK_PROVIDERS,
    MAX_TASK_SECONDS,
    PROVIDER_COOLDOWN_SECONDS,
    ProviderHealth,
    RouteCandidate,
    RouterSettings,
    classify_task,
    fallback_reason,
    public_health,
    rank_providers,
)

LOGGER = logging.getLogger(__name__)
TASK_HISTORY_LIMIT = 50
MAX_ATTEMPT_HISTORY = 100
MAX_CLOUDFLARE_DAILY_NEURONS = 8_000
TASK_TIMEOUT_MESSAGE = "Se alcanzó el límite total de tiempo de la tarea."


@dataclass(frozen=True)
class SmartTask:
    id: str
    instruction: str
    provider_id: str | None
    status: str
    created_at: str
    updated_at: str
    result: str | None
    error: str | None
    allowed_provider_ids: tuple[str, ...] = ()
    external_consent: bool = False
    selected_model: str | None = None


class SmartTaskStore:
    """Extend the existing local task database with queue and router state."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = (
            Path(database_path).expanduser()
            if database_path is not None
            else default_state_directory() / "tasks.sqlite3"
        )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        if os.name != "nt":
            self.database_path.chmod(0o600)
        return connection

    def _initialize(self) -> None:
        # Preserve and extend existing V3 history without modifying its public engine.
        from aibridge.agents import TaskStore

        base_store = TaskStore(self.database_path)
        with closing(base_store._connect()) as connection, connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
            }
            migrations = {
                "allowed_provider_ids": "TEXT NOT NULL DEFAULT '[]'",
                "external_consent": "INTEGER NOT NULL DEFAULT 0",
                "selected_model": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reason TEXT,
                    latency_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_usage (
                    provider_id TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    requests INTEGER NOT NULL,
                    estimated_tokens INTEGER NOT NULL,
                    estimated_neurons INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (provider_id, usage_date)
                )
                """
            )
            usage_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(provider_usage)")
            }
            if "estimated_neurons" not in usage_columns:
                connection.execute(
                    "ALTER TABLE provider_usage ADD COLUMN estimated_neurons INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS router_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    settings_json TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _task(row: sqlite3.Row) -> SmartTask:
        try:
            provider_ids = json.loads(row["allowed_provider_ids"])
        except (json.JSONDecodeError, TypeError):
            provider_ids = []
        if not isinstance(provider_ids, list):
            provider_ids = []
        return SmartTask(
            id=row["id"],
            instruction=row["instruction"],
            provider_id=row["provider_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result=row["result"],
            error=row["error"],
            allowed_provider_ids=tuple(
                item for item in provider_ids if isinstance(item, str)
            ),
            external_consent=bool(row["external_consent"]),
            selected_model=row["selected_model"],
        )

    def create(self, instruction: str) -> SmartTask:
        task = SmartTask(
            id=uuid.uuid4().hex,
            instruction=instruction,
            provider_id=None,
            status="awaiting_approval",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            result=None,
            error=None,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO tasks
                    (id, instruction, provider_id, status, created_at, updated_at,
                     result, error, allowed_provider_ids, external_consent, selected_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, NULL)
                """,
                (
                    task.id,
                    task.instruction,
                    None,
                    task.status,
                    task.created_at,
                    task.updated_at,
                    None,
                    None,
                ),
            )
        return task

    def get(self, task_id: str) -> SmartTask:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFound("No se encontró la tarea.")
        return self._task(row)

    def list_recent(self) -> list[SmartTask]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (TASK_HISTORY_LIMIT,),
            ).fetchall()
        return [self._task(row) for row in rows]

    def approve(
        self, task_id: str, provider_ids: Sequence[str], external_consent: bool
    ) -> SmartTask:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET status = 'approved', provider_id = ?,
                    allowed_provider_ids = ?, external_consent = ?,
                    result = NULL, error = NULL, updated_at = ?
                WHERE id = ? AND status IN ('awaiting_approval', 'failed')
                """,
                (
                    provider_ids[0],
                    json.dumps(list(provider_ids)),
                    int(external_consent),
                    now,
                    task_id,
                ),
            )
            if cursor.rowcount == 0:
                if connection.execute(
                    "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
                ).fetchone() is None:
                    raise TaskNotFound("No se encontró la tarea.")
                raise TaskConflict("La tarea no está pendiente ni puede reintentarse.")
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._task(row)

    def claim_next(self) -> SmartTask | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM tasks WHERE status = 'approved' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            task_id = row["id"]
            cursor = connection.execute(
                "UPDATE tasks SET status = 'running', updated_at = ? "
                "WHERE id = ? AND status = 'approved'",
                (datetime.now(timezone.utc).isoformat(), task_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            connection.commit()
        return self._task(task)

    def has_approved_tasks(self) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM tasks WHERE status = 'approved' LIMIT 1"
            ).fetchone()
        return row is not None

    def complete(
        self,
        task_id: str,
        *,
        result: str | None = None,
        error: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
    ) -> SmartTask:
        status = "completed" if error is None else "failed"
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET status = ?, updated_at = ?, result = ?, error = ?,
                    provider_id = COALESCE(?, provider_id),
                    selected_model = COALESCE(?, selected_model)
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    result,
                    error,
                    provider_id,
                    model,
                    task_id,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskConflict("No se pudo guardar el resultado de la tarea.")
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._task(row)

    def recover_interrupted(self) -> int:
        """Do not replay uncertain provider calls; ask for explicit re-approval."""
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET status = 'failed', updated_at = ?,
                    error = 'Ejecución interrumpida; revisa la tarea y vuelve a aprobarla.'
                WHERE status = 'running'
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )
        return cursor.rowcount

    def record_attempt(
        self,
        task_id: str,
        provider_id: str,
        model: str,
        outcome: str,
        reason: str | None,
        latency_ms: int,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO provider_attempts
                    (task_id, provider_id, model, outcome, reason, latency_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    provider_id,
                    model,
                    outcome,
                    reason,
                    max(0, latency_ms),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def attempt_history(self) -> list[dict[str, object]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT task_id, provider_id, model, outcome, reason, latency_ms, created_at
                FROM provider_attempts ORDER BY id DESC LIMIT ?
                """,
                (MAX_ATTEMPT_HISTORY,),
            ).fetchall()
        return [dict(row) for row in rows]

    def usage(self) -> dict[str, tuple[int, int, int]]:
        day = datetime.now(timezone.utc).date().isoformat()
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT provider_id, requests, estimated_tokens, estimated_neurons "
                "FROM provider_usage WHERE usage_date = ?",
                (day,),
            ).fetchall()
        return {
            row["provider_id"]: (
                row["requests"],
                row["estimated_tokens"],
                row["estimated_neurons"],
            )
            for row in rows
        }

    def reserve(
        self,
        provider_id: str,
        estimated_tokens: int,
        *,
        neuron_budget: int | None = None,
        estimated_neurons: int = 0,
    ) -> bool:
        day = datetime.now(timezone.utc).date().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT requests, estimated_tokens, estimated_neurons "
                "FROM provider_usage WHERE provider_id = ? AND usage_date = ?",
                (provider_id, day),
            ).fetchone()
            requests, tokens, neurons = (
                (row["requests"], row["estimated_tokens"], row["estimated_neurons"])
                if row
                else (0, 0, 0)
            )
            if (
                requests >= LOCAL_DAILY_REQUEST_LIMIT
                or tokens + estimated_tokens > LOCAL_DAILY_TOKEN_LIMIT
                or (
                    neuron_budget is not None
                    and neurons + estimated_neurons > neuron_budget
                )
            ):
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO provider_usage
                    (provider_id, usage_date, requests, estimated_tokens, estimated_neurons)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(provider_id, usage_date) DO UPDATE SET
                    requests = requests + 1,
                    estimated_tokens = estimated_tokens + excluded.estimated_tokens,
                    estimated_neurons = estimated_neurons + excluded.estimated_neurons
                """,
                (provider_id, day, estimated_tokens, estimated_neurons),
            )
            connection.commit()
        return True

    def router_settings(self) -> RouterSettings | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT settings_json FROM router_settings WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            return RouterSettings.from_value(json.loads(row["settings_json"]))
        except json.JSONDecodeError:
            return None

    def save_router_settings(self, settings: RouterSettings) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO router_settings (id, settings_json) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET settings_json = excluded.settings_json
                """,
                (json.dumps(settings.as_dict()),),
            )


class SmartTaskEngine:
    """Asynchronous, approval-gated router; it never executes model output."""

    def __init__(
        self,
        store: SmartTaskStore,
        providers: dict[str, AgentProvider],
        statuses: tuple[ProviderStatus, ...],
        sensitive_values: tuple[str, ...],
        *,
        cloudflare_budget: int | None = None,
    ) -> None:
        self.store = store
        self.providers = dict(providers)
        self.provider_statuses = statuses
        self.sensitive_values = tuple(value for value in sensitive_values if value)
        self.cloudflare_budget = cloudflare_budget
        self.health: dict[str, ProviderHealth] = {}
        self._worker_lock = threading.Lock()
        self._worker_stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.store.recover_interrupted()
        for attempt in reversed(self.store.attempt_history()):
            state = self.health.setdefault(attempt["provider_id"], ProviderHealth())
            if attempt["outcome"] == "completed":
                state.successes += 1
                state.latency_ms = float(attempt["latency_ms"])
            else:
                state.failures += 1
                state.last_error = attempt["reason"]
                state.last_failure_kind = "historical"
        if self.store.router_settings() is None:
            self.store.save_router_settings(
                RouterSettings(
                    enabled_provider_ids=tuple(
                        status.identifier for status in statuses if status.enabled
                    )
                )
            )
        if self.store.has_approved_tasks():
            self.start_worker()

    def create_task(self, instruction: str) -> SmartTask:
        if not isinstance(instruction, str) or not instruction.strip():
            raise TaskValidationError("Escribe una instrucción antes de crear la tarea.")
        instruction = instruction.strip()
        if len(instruction) > 8_000:
            raise TaskValidationError("La instrucción no puede superar 8000 caracteres.")
        if any(secret in instruction for secret in self.sensitive_values):
            raise TaskValidationError(
                "La instrucción contiene una credencial configurada. Elimínala antes de continuar."
            )
        return self.store.create(instruction)

    def route_plan(self, instruction: str) -> list[RouteCandidate]:
        settings = self.store.router_settings() or RouterSettings()
        usage = {key: (value[0], value[1]) for key, value in self.store.usage().items()}
        return rank_providers(
            instruction,
            self._current_statuses(),
            settings,
            self.health,
            now=time.monotonic(),
            usage=usage,
        )

    def update_router_settings(self, payload: object) -> RouterSettings:
        if not isinstance(payload, dict):
            raise TaskValidationError("La configuración del router debe ser un objeto.")
        known_ids = {status.identifier for status in self.provider_statuses}
        mode = payload.get("mode")
        manual_id = payload.get("manual_provider_id")
        enabled_ids = payload.get("enabled_provider_ids")
        priorities = payload.get("priorities", {})
        if mode not in {"automatic", "manual"}:
            raise TaskValidationError("El modo debe ser automatic o manual.")
        if not isinstance(enabled_ids, list) or any(
            not isinstance(item, str) or item not in known_ids for item in enabled_ids
        ):
            raise TaskValidationError("La lista de proveedores habilitados no es válida.")
        if len(set(enabled_ids)) != len(enabled_ids):
            raise TaskValidationError("La lista de proveedores no puede tener duplicados.")
        if not isinstance(priorities, dict) or any(
            not isinstance(key, str)
            or key not in known_ids
            or not isinstance(value, int)
            or isinstance(value, bool)
            or not -10 <= value <= 10
            for key, value in priorities.items()
        ):
            raise TaskValidationError("Las prioridades deben estar entre -10 y 10.")
        if manual_id is not None and (
            not isinstance(manual_id, str) or manual_id not in enabled_ids
        ):
            raise TaskValidationError("El proveedor manual debe estar habilitado.")
        if mode == "manual" and manual_id is None:
            raise TaskValidationError("Selecciona un proveedor para el modo manual.")
        settings = RouterSettings(
            mode=mode,
            manual_provider_id=manual_id,
            enabled_provider_ids=tuple(enabled_ids),
            priorities=priorities,
        )
        self.store.save_router_settings(settings)
        return settings

    def approve_task(
        self,
        task_id: str,
        *,
        provider_ids: Sequence[str] | None = None,
        external_consent: bool = False,
    ) -> SmartTask:
        task = self.store.get(task_id)
        plan = self.route_plan(task.instruction)
        planned_ids = [candidate.provider_id for candidate in plan]
        approved_ids = list(provider_ids) if provider_ids is not None else planned_ids
        if (
            not approved_ids
            or len(set(approved_ids)) != len(approved_ids)
            or any(provider_id not in planned_ids for provider_id in approved_ids)
        ):
            raise AgentUnavailable(
                "No hay proveedores gratuitos disponibles o la selección no coincide con el plan."
            )
        if not external_consent:
            raise TaskValidationError(
                "Confirma que autorizas enviar la instrucción a los proveedores seleccionados."
            )
        return self.store.approve(task_id, approved_ids, external_consent)

    def run_next_task(self) -> bool:
        task = self.store.claim_next()
        if task is None:
            return False
        self._execute(task)
        return True

    def start_worker(self) -> None:
        """Start the single local queue worker after an authorized task is approved."""
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker_stop.clear()
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="aibridge-smart-router",
                daemon=True,
            )
            self._worker.start()

    def stop_worker(self) -> None:
        self._worker_stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2)

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            if not self.run_next_task():
                self._worker_stop.wait(0.25)

    def _execute(self, task: SmartTask) -> None:
        task_started = time.monotonic()
        latest_error = "Fallaron todos los proveedores gratuitos autorizados."
        last_provider: str | None = None
        route_reasons = {
            candidate.provider_id: candidate.explanation
            for candidate in self.route_plan(task.instruction)
        }
        fallback_note: str | None = None
        for provider_id in task.allowed_provider_ids[:MAX_FALLBACK_PROVIDERS]:
            if time.monotonic() - task_started >= MAX_TASK_SECONDS:
                latest_error = TASK_TIMEOUT_MESSAGE
                break
            provider = self.providers.get(provider_id)
            status = next(
                (item for item in self._current_statuses() if item.identifier == provider_id),
                None,
            )
            if provider is None or status is None or not status.enabled:
                latest_error = "Un proveedor autorizado ya no está habilitado."
                continue
            prompt_tokens = math.ceil(len(task.instruction) / 3)
            output_tokens = 256 if provider_id == "cloudflare" else MAX_ESTIMATED_OUTPUT_TOKENS
            estimated_tokens = prompt_tokens + output_tokens
            estimated_neurons = self._cloudflare_neurons(prompt_tokens, output_tokens)
            neuron_budget = (
                self.cloudflare_budget if provider_id == "cloudflare" else None
            )
            if not self.store.reserve(
                provider_id,
                estimated_tokens,
                neuron_budget=neuron_budget,
                estimated_neurons=estimated_neurons,
            ):
                latest_error = "El límite local diario impide otra solicitud."
                continue
            last_provider = provider_id
            attempt_started = time.monotonic()
            try:
                result = provider.generate(task.instruction)
            except ProviderError as error:
                latency_ms = int((time.monotonic() - attempt_started) * 1000)
                reason = (
                    fallback_reason(provider_id, error.kind)
                    if error.retryable
                    else f"{provider_id}: {str(error)}"
                )
                self.store.record_attempt(
                    task.id,
                    provider_id,
                    status.model or "",
                    "failed",
                    reason,
                    latency_ms,
                )
                self._record_failure(provider_id, error)
                latest_error = str(error)
                if error.kind == "credentials":
                    self._disable_provider(
                        provider_id,
                        "Credenciales inválidas; corrige la configuración del servidor.",
                    )
                    break
                if error.retryable:
                    fallback_note = fallback_reason(provider_id, error.kind)
                    continue
                break
            latency_ms = int((time.monotonic() - attempt_started) * 1000)
            for secret in self.sensitive_values:
                result = result.replace(secret, "[API KEY REDACTED]")
            self._record_success(provider_id, latency_ms)
            self.store.record_attempt(
                task.id,
                provider_id,
                getattr(provider, "selected_model", status.model) or "",
                "completed",
                (
                    f"{fallback_note}; siguiente proveedor autorizado. "
                    if fallback_note
                    else ""
                )
                + route_reasons.get(
                    provider_id,
                    "Proveedor autorizado al aprobar la tarea; no hay métricas de calidad medidas.",
                ),
                latency_ms,
            )
            self.store.complete(
                task.id,
                result=result,
                provider_id=provider_id,
                model=getattr(provider, "selected_model", status.model),
            )
            return
        final_error = latest_error
        if last_provider is None and latest_error == "Fallaron todos los proveedores gratuitos autorizados.":
            final_error = "No quedan solicitudes gratuitas disponibles para esta tarea."
        self.store.complete(task.id, error=final_error)

    @staticmethod
    def _cloudflare_neurons(prompt_tokens: int, output_tokens: int) -> int:
        if prompt_tokens <= 0:
            return 0
        # Official published rates for @cf/meta/llama-3.2-1b-instruct,
        # doubled locally to leave a safety margin for tokenizer differences.
        return math.ceil(
            2 * (prompt_tokens * 2457 + output_tokens * 18252) / 1_000_000
        )

    def _record_failure(self, provider_id: str, error: ProviderError) -> None:
        state = self.health.setdefault(provider_id, ProviderHealth())
        state.failures += 1
        state.last_error = str(error)
        state.last_failure_kind = error.kind
        if error.retryable:
            state.available_at = time.monotonic() + PROVIDER_COOLDOWN_SECONDS

    def _record_success(self, provider_id: str, latency_ms: int) -> None:
        state = self.health.setdefault(provider_id, ProviderHealth())
        state.successes += 1
        state.available_at = 0
        state.last_error = None
        state.last_failure_kind = None
        state.latency_ms = float(latency_ms)

    def _disable_provider(self, provider_id: str, message: str) -> None:
        self.provider_statuses = tuple(
            ProviderStatus(
                identifier=status.identifier,
                display_name=status.display_name,
                configured=False if status.identifier == provider_id else status.configured,
                enabled=False if status.identifier == provider_id else status.enabled,
                simulation=status.simulation,
                message=message if status.identifier == provider_id else status.message,
                model=status.model,
                availability="disabled" if status.identifier == provider_id else status.availability,
                quota=status.quota,
                free_only=status.free_only,
                capabilities=status.capabilities,
                context_window_tokens=status.context_window_tokens,
            )
            for status in self.provider_statuses
        )
        self.providers.pop(provider_id, None)

    def _current_statuses(self) -> tuple[ProviderStatus, ...]:
        settings = self.store.router_settings() or RouterSettings()
        active = set(settings.enabled_provider_ids)
        return tuple(
            ProviderStatus(
                identifier=status.identifier,
                display_name=status.display_name,
                configured=status.configured,
                enabled=status.enabled and status.identifier in active,
                simulation=status.simulation,
                message=status.message,
                model=status.model,
                availability=status.availability,
                quota=status.quota,
                free_only=status.free_only,
                capabilities=status.capabilities,
                context_window_tokens=status.context_window_tokens,
            )
            for status in self.provider_statuses
        )

    def task_history(self) -> list[SmartTask]:
        return self.store.list_recent()

    def public_status(self) -> dict[str, object]:
        usage = self.store.usage()
        statuses = []
        settings = self.store.router_settings() or RouterSettings()
        active_ids = set(settings.enabled_provider_ids)
        for status in self.provider_statuses:
            requests, tokens, neurons = usage.get(status.identifier, (0, 0, 0))
            statuses.append(
                {
                    "id": status.identifier,
                    "name": status.display_name,
                    "model": status.model,
                    "configured": status.configured,
                    "eligible": status.enabled,
                    "enabled": status.enabled and status.identifier in active_ids,
                    "simulation": status.simulation,
                    "message": status.message,
                    "availability": status.availability,
                    "quota": status.quota,
                    "free_only": status.free_only,
                    "capabilities": list(status.capabilities),
                    "context_window_tokens": status.context_window_tokens,
                    "local_requests_today": requests,
                    "local_estimated_tokens_today": tokens,
                    "local_estimated_neurons_today": neurons,
                }
            )
        selected = next((status for status in statuses if status["enabled"]), None)
        return {
            "free_only": True,
            "mode": settings.mode,
            "settings": settings.as_dict(),
            "provider": selected
            or {
                "id": None,
                "name": "Ningún proveedor gratuito disponible",
                "configured": False,
                "enabled": False,
                "simulation": False,
                "message": "Configura una ruta gratuita en el entorno del servidor.",
            },
            "providers": statuses,
            "health": public_health(self.health),
            "attempts": self.store.attempt_history(),
        }


def create_smart_engine(
    database_path: str | Path | None = None,
) -> SmartTaskEngine:
    """Create the production router using only opt-in free-tier configurations."""
    config = configure_free_providers()
    try:
        neuron_budget = int(os.environ.get("AI_BRIDGE_CLOUDFLARE_DAILY_NEURON_BUDGET", "0"))
    except ValueError:
        neuron_budget = 0
    return SmartTaskEngine(
        SmartTaskStore(database_path),
        config.providers,
        config.statuses,
        config.sensitive_values,
        cloudflare_budget=neuron_budget or None,
    )
