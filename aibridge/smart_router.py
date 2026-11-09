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
    model_route_id,
    provider_request_timeout,
)
from aibridge.router import (
    LOCAL_DAILY_REQUEST_LIMIT,
    LOCAL_DAILY_TOKEN_LIMIT,
    MAX_ESTIMATED_OUTPUT_TOKENS,
    MAX_FALLBACK_PROVIDERS,
    MAX_FALLBACK_ROUTES,
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
                    provider_ids[0].split("::", 1)[0],
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
        if error is None and (not isinstance(result, str) or not result.strip()):
            raise TaskValidationError(
                "Una tarea solo puede completarse con una respuesta de texto válida."
            )
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
            route_id = model_route_id(attempt["provider_id"], attempt["model"])
            state = self.health.setdefault(route_id, ProviderHealth())
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
                        dict.fromkeys(
                            status.identifier
                            for status in statuses
                            if status.enabled and status.free_only
                        )
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

    @staticmethod
    def _provider_id_from_route(route_id: str) -> str:
        return route_id.split("::", 1)[0]

    def _find_status(self, route_id: str) -> ProviderStatus | None:
        return next(
            (
                status
                for status in self._current_statuses()
                if status.route_id == route_id or status.identifier == route_id
            ),
            None,
        )

    def _find_provider(
        self, route_id: str, status: ProviderStatus | None
    ) -> AgentProvider | None:
        provider = self.providers.get(route_id)
        if provider is None and status is not None:
            provider = self.providers.get(status.identifier)
        if provider is None and "::" in route_id:
            provider = self.providers.get(self._provider_id_from_route(route_id))
        return provider

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
        planned_ids = [candidate.route_id for candidate in plan]
        approved_ids = list(provider_ids) if provider_ids is not None else planned_ids
        planned_by_provider: dict[str, list[str]] = {}
        for candidate in plan:
            planned_by_provider.setdefault(candidate.provider_id, []).append(
                candidate.route_id
            )
        normalized_ids = []
        for route_id in approved_ids:
            if route_id in planned_ids:
                normalized_ids.append(route_id)
            elif route_id in planned_by_provider and len(
                planned_by_provider[route_id]
            ) == 1:
                normalized_ids.append(planned_by_provider[route_id][0])
            else:
                raise AgentUnavailable(
                    "No hay proveedores gratuitos disponibles o la selección no coincide con el plan."
                )
        if (
            not normalized_ids
            or len(set(normalized_ids)) != len(normalized_ids)
            or len(normalized_ids) > MAX_FALLBACK_ROUTES
            or len(
                {
                    self._provider_id_from_route(route_id)
                    for route_id in normalized_ids
                }
            )
            > MAX_FALLBACK_PROVIDERS
        ):
            raise AgentUnavailable(
                "No hay proveedores gratuitos disponibles o la selección no coincide con el plan."
            )
        if not external_consent:
            raise TaskValidationError(
                "Confirma que autorizas enviar la instrucción a los proveedores seleccionados."
            )
        return self.store.approve(task_id, normalized_ids, external_consent)

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

    def _redact_sensitive(self, value: str) -> str:
        for secret in self.sensitive_values:
            value = value.replace(secret, "[API KEY REDACTED]")
        return value

    def _unavailable_route_diagnostics(self, task: SmartTask) -> list[str]:
        settings = self.store.router_settings() or RouterSettings()
        enabled_ids = set(settings.enabled_provider_ids)
        usage = self.store.usage()
        estimated_prompt_tokens = math.ceil(len(task.instruction) / 3)
        now = time.monotonic()
        diagnostics = []
        for status in self.provider_statuses:
            if status.route_id in task.allowed_provider_ids or (
                status.identifier in task.allowed_provider_ids
            ):
                continue
            if not status.free_only:
                reason = "Bloqueado por FREE_ONLY."
            elif not status.enabled:
                reason = status.message
            elif status.simulation:
                reason = "La ruta de simulación no está permitida para tareas externas."
            elif status.identifier not in enabled_ids:
                reason = "Deshabilitado en las preferencias del router."
            elif self._find_provider(status.route_id, status) is None:
                reason = "El adaptador del proveedor no está disponible."
            elif "text" not in status.capabilities:
                reason = "El proveedor no admite texto."
            elif (
                status.context_window_tokens is not None
                and estimated_prompt_tokens + MAX_ESTIMATED_OUTPUT_TOKENS
                > status.context_window_tokens
            ):
                reason = "La instrucción supera la ventana de contexto disponible."
            else:
                health = self.health.get(status.route_id)
                requests, tokens, _ = usage.get(status.identifier, (0, 0, 0))
                if health is not None and health.available_at > now:
                    seconds = max(1, int(health.available_at - now))
                    reason = f"En enfriamiento local; disponible en aproximadamente {seconds} s."
                elif (
                    requests >= LOCAL_DAILY_REQUEST_LIMIT
                    or tokens + MAX_ESTIMATED_OUTPUT_TOKENS
                    >= LOCAL_DAILY_TOKEN_LIMIT
                ):
                    reason = "Se alcanzó el límite local diario de solicitudes o tokens."
                else:
                    reason = "Ruta gratuita elegible, pero no autorizada para esta tarea."
            model = status.model or "modelo no verificado"
            diagnostics.append(
                self._redact_sensitive(
                    f"{status.identifier} ({model}): {reason}"
                )
            )
        return diagnostics

    def _execute(self, task: SmartTask) -> None:
        task_started = time.monotonic()
        route_diagnostics: list[str] = []
        attempted_count = 0
        final_summary = "Fallaron todos los modelos gratuitos aprobados."
        route_reasons = {
            candidate.route_id: candidate.explanation
            for candidate in self.route_plan(task.instruction)
        }
        fallback_note: str | None = None
        approved_ids = list(task.allowed_provider_ids)
        candidate_ids = approved_ids[:MAX_FALLBACK_ROUTES]
        for index, provider_id in enumerate(candidate_ids):
            if time.monotonic() - task_started >= MAX_TASK_SECONDS:
                final_summary = TASK_TIMEOUT_MESSAGE
                route_diagnostics.extend(
                    f"{remaining_id}: no intentado por el límite total de tiempo."
                    for remaining_id in candidate_ids[index:]
                )
                break
            route_id = provider_id
            status = self._find_status(route_id)
            if status is None and "::" not in route_id:
                status = next(
                    (item for item in self._current_statuses() if item.identifier == route_id),
                    None,
                )
            provider = self._find_provider(route_id, status)
            base_provider_id = (
                status.identifier
                if status is not None
                else self._provider_id_from_route(route_id)
            )
            original_status = next(
                (item for item in self.provider_statuses if item.route_id == route_id),
                None,
            ) or next(
                (item for item in self.provider_statuses if item.identifier == base_provider_id),
                None,
            )
            model = (
                status.model if status is not None else None
            ) or (
                getattr(provider, "selected_model", None) if provider is not None else None
            ) or (route_id.split("::", 1)[1] if "::" in route_id else "modelo no verificado")
            if status is not None and not status.free_only:
                route_diagnostics.append(
                    f"{provider_id} ({model}): bloqueado por FREE_ONLY; no se envió."
                )
                continue
            if provider is None or status is None or not status.enabled:
                reason = (
                    status.message
                    if status is not None
                    else original_status.message
                    if original_status is not None
                    else "El proveedor ya no está configurado."
                )
                route_diagnostics.append(
                    self._redact_sensitive(f"{provider_id} ({model}): no intentado; {reason}")
                )
                continue
            prompt_tokens = math.ceil(len(task.instruction) / 3)
            output_tokens = status.max_output_tokens
            estimated_tokens = prompt_tokens + output_tokens
            estimated_neurons = self._cloudflare_neurons(prompt_tokens, output_tokens)
            neuron_budget = (
                self.cloudflare_budget if base_provider_id == "cloudflare" else None
            )
            if not self.store.reserve(
                base_provider_id,
                estimated_tokens,
                neuron_budget=neuron_budget,
                estimated_neurons=estimated_neurons,
            ):
                route_diagnostics.append(
                    f"{provider_id} ({model}): no intentado; el límite local diario "
                    "impide otra solicitud."
                )
                continue
            attempted_count += 1
            attempt_started = time.monotonic()
            try:
                remaining_seconds = MAX_TASK_SECONDS - (
                    attempt_started - task_started
                )
                with provider_request_timeout(remaining_seconds):
                    result = provider.generate(task.instruction)
            except ProviderError as error:
                latency_ms = int((time.monotonic() - attempt_started) * 1000)
                safe_message = self._redact_sensitive(str(error))
                detail = f"{base_provider_id} ({model}): {error.kind}"
                if error.http_status is not None:
                    detail += f" (HTTP {error.http_status})"
                detail += f" — {safe_message}"
                if error.retryable:
                    reason = (
                        f"{detail}; {fallback_reason(base_provider_id, error.kind)}"
                    )
                    route_diagnostics.append(
                        f"{detail}; se intentará la siguiente ruta aprobada."
                    )
                else:
                    reason = detail
                    route_diagnostics.append(detail)
                self.store.record_attempt(
                    task.id,
                    base_provider_id,
                    model,
                    "failed",
                    reason,
                    latency_ms,
                )
                self._record_failure(route_id, error)
                if error.kind == "credentials":
                    self._disable_provider(
                        base_provider_id,
                        "Credenciales inválidas; corrige la configuración del servidor.",
                    )
                    final_summary = (
                        "Se detuvo el fallback por credenciales inválidas; "
                        "no se enviará la tarea a otro proveedor."
                    )
                    route_diagnostics.extend(
                        f"{remaining_id}: no intentado por la política de credenciales."
                        for remaining_id in candidate_ids[index + 1 :]
                    )
                    break
                if error.retryable:
                    if error.kind == "access_blocked":
                        self._disable_provider(
                            base_provider_id,
                            "Ruta suspendida por bloqueo de acceso; no se intentará eludirlo.",
                        )
                    fallback_note = fallback_reason(base_provider_id, error.kind)
                    continue
                final_summary = (
                    "Se detuvo el fallback porque el error no admite reintentos "
                    "con otro proveedor."
                )
                route_diagnostics.extend(
                    f"{remaining_id}: no intentado por la política de no reintentar "
                    f"errores {error.kind}."
                    for remaining_id in candidate_ids[index + 1 :]
                )
                break
            latency_ms = int((time.monotonic() - attempt_started) * 1000)
            if not isinstance(result, str) or not result.strip():
                error = ProviderError(
                    "El proveedor no devolvió una respuesta de texto válida.",
                    kind="invalid_response",
                )
                self.store.record_attempt(
                    task.id,
                    base_provider_id,
                    model,
                    "failed",
                    self._redact_sensitive(str(error)),
                    latency_ms,
                )
                self._record_failure(route_id, error)
                route_diagnostics.append(
                    f"{base_provider_id} ({model}): invalid_response — "
                    f"{self._redact_sensitive(str(error))}; se intentará la siguiente ruta aprobada."
                )
                fallback_note = fallback_reason(base_provider_id, error.kind)
                continue
            result = self._redact_sensitive(result)
            self._record_success(route_id, latency_ms)
            self.store.record_attempt(
                task.id,
                base_provider_id,
                getattr(provider, "selected_model", status.model) or "",
                "completed",
                (
                    f"{fallback_note}; siguiente ruta aprobada. "
                    if fallback_note
                    else ""
                )
                + route_reasons.get(
                    route_id,
                    "Proveedor autorizado al aprobar la tarea; no hay métricas de calidad medidas.",
                ),
                latency_ms,
            )
            self.store.complete(
                task.id,
                result=result,
                provider_id=base_provider_id,
                model=getattr(provider, "selected_model", status.model),
            )
            return
        if attempted_count == 0 and final_summary == "Fallaron todos los modelos gratuitos aprobados.":
            final_summary = "Ninguna ruta gratuita aprobada pudo iniciar una solicitud."
        route_diagnostics.extend(
            f"{provider_id}: no intentado porque se excede el máximo de "
            f"{MAX_FALLBACK_ROUTES} rutas por tarea."
            for provider_id in approved_ids[MAX_FALLBACK_ROUTES:]
        )
        unavailable_routes = self._unavailable_route_diagnostics(task)
        diagnostic_sections = [final_summary]
        if route_diagnostics:
            diagnostic_sections.extend(
                ["Diagnóstico de rutas aprobadas:", *[f"- {line}" for line in route_diagnostics]]
            )
        if unavailable_routes:
            diagnostic_sections.extend(
                [
                    "Otras rutas gratuitas no disponibles o no autorizadas:",
                    *[f"- {line}" for line in unavailable_routes],
                ]
            )
        final_error = self._redact_sensitive("\n".join(diagnostic_sections))
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
        state.last_error = self._redact_sensitive(str(error))
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
                max_output_tokens=status.max_output_tokens,
                local_daily_request_limit=status.local_daily_request_limit,
                local_daily_token_limit=status.local_daily_token_limit,
            )
            for status in self.provider_statuses
        )
        for route_id in tuple(self.providers):
            if route_id == provider_id or route_id.startswith(f"{provider_id}::"):
                self.providers.pop(route_id, None)

    def _current_statuses(self) -> tuple[ProviderStatus, ...]:
        settings = self.store.router_settings() or RouterSettings()
        active = set(settings.enabled_provider_ids)
        return tuple(
            ProviderStatus(
                identifier=status.identifier,
                display_name=status.display_name,
                configured=status.configured,
                enabled=status.enabled
                and status.free_only
                and status.identifier in active,
                simulation=status.simulation,
                message=status.message,
                model=status.model,
                availability=status.availability,
                quota=status.quota,
                free_only=status.free_only,
                capabilities=status.capabilities,
                context_window_tokens=status.context_window_tokens,
                max_output_tokens=status.max_output_tokens,
                local_daily_request_limit=status.local_daily_request_limit,
                local_daily_token_limit=status.local_daily_token_limit,
            )
            for status in self.provider_statuses
        )

    def task_history(self) -> list[SmartTask]:
        return self.store.list_recent()

    def public_status(self) -> dict[str, object]:
        usage = self.store.usage()
        settings = self.store.router_settings() or RouterSettings()
        active_ids = set(settings.enabled_provider_ids)
        health_by_route = {
            entry["route_id"]: entry for entry in public_health(self.health)
        }
        groups: dict[str, list[ProviderStatus]] = {}
        for status in self.provider_statuses:
            groups.setdefault(status.identifier, []).append(status)
        statuses = []
        for provider_id, provider_models in groups.items():
            requests, tokens, neurons = usage.get(provider_id, (0, 0, 0))
            models = []
            for status in provider_models:
                health = health_by_route.get(status.route_id)
                models.append(
                    {
                        "route_id": status.route_id,
                        "model": status.model if status.free_only else None,
                        "configured": status.configured,
                        "eligible": status.enabled and status.free_only,
                        "enabled": status.enabled
                        and status.free_only
                        and provider_id in active_ids,
                        "message": status.message
                        if status.free_only
                        else "Deshabilitado por FREE_ONLY; modelo no verificado.",
                        "availability": (
                            health["state"] if health else status.availability
                        ),
                        "capabilities": list(status.capabilities),
                        "context_window_tokens": status.context_window_tokens,
                        "max_output_tokens": status.max_output_tokens,
                        "local_daily_request_limit": status.local_daily_request_limit,
                        "local_daily_token_limit": status.local_daily_token_limit,
                        "health": health,
                    }
                )
            eligible_models = [
                model for model in models if model["eligible"] and model["model"]
            ]
            capabilities = sorted(
                {
                    capability
                    for model in eligible_models
                    for capability in model["capabilities"]
                }
            )
            statuses.append(
                {
                    "id": provider_id,
                    "name": provider_models[0].display_name,
                    "model": (
                        eligible_models[0]["model"]
                        if len(eligible_models) == 1
                        else None
                    ),
                    "models": models,
                    "configured": any(status.configured for status in provider_models),
                    "eligible": bool(eligible_models),
                    "enabled": bool(eligible_models) and provider_id in active_ids,
                    "simulation": all(status.simulation for status in provider_models),
                    "message": (
                        next(
                            (status.message for status in provider_models if status.enabled),
                            provider_models[0].message,
                        )
                    ),
                    "availability": (
                        "available" if eligible_models else "disabled"
                    ),
                    "quota": provider_models[0].quota,
                    "free_only": all(status.free_only for status in provider_models),
                    "capabilities": capabilities,
                    "context_window_tokens": (
                        eligible_models[0]["context_window_tokens"]
                        if len(eligible_models) == 1
                        else None
                    ),
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
