"""Local task history and approval-gated agent execution."""

import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from aibridge.providers import (
    AgentProvider,
    ProviderError,
    ProviderStatus,
    configured_api_keys,
)

MAX_INSTRUCTION_LENGTH = 8_000
TASK_HISTORY_LIMIT = 50


class AgentUnavailable(RuntimeError):
    """Raised when no approved and configured provider is available."""


class TaskNotFound(LookupError):
    """Raised when a task identifier is unknown."""


class TaskConflict(RuntimeError):
    """Raised when a task is not in a state that permits the requested action."""


class TaskValidationError(ValueError):
    """Raised when a task instruction does not meet the input constraints."""


@dataclass(frozen=True)
class AgentTask:
    id: str
    instruction: str
    provider_id: str | None
    status: str
    created_at: str
    updated_at: str
    result: str | None
    error: str | None


def default_state_directory() -> Path:
    """Return a per-user state directory without placing history in the workspace."""
    override = os.environ.get("AI_BRIDGE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "AI-Bridge"
        return Path.home() / "AppData" / "Local" / "AI-Bridge"
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home).expanduser() / "ai-bridge"
    return Path.home() / ".local" / "state" / "ai-bridge"


class TaskStore:
    """Persist task prompts, status, and text results in a local SQLite database."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = (
            Path(database_path).expanduser()
            if database_path is not None
            else default_state_directory() / "tasks.sqlite3"
        )

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        if os.name != "nt":
            self.database_path.chmod(0o600)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                instruction TEXT NOT NULL,
                provider_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                result TEXT,
                error TEXT
            )
            """
        )
        return connection

    @staticmethod
    def _task(row: sqlite3.Row) -> AgentTask:
        return AgentTask(**dict(row))

    def create(self, instruction: str, provider_id: str | None) -> AgentTask:
        now = datetime.now(timezone.utc).isoformat()
        task = AgentTask(
            id=uuid.uuid4().hex,
            instruction=instruction,
            provider_id=provider_id,
            status="awaiting_approval",
            created_at=now,
            updated_at=now,
            result=None,
            error=None,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO tasks
                    (id, instruction, provider_id, status, created_at, updated_at, result, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(asdict(task).values()),
            )
        return task

    def get(self, task_id: str) -> AgentTask:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFound("No se encontró la tarea.")
        return self._task(row)

    def list_recent(self) -> list[AgentTask]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (TASK_HISTORY_LIMIT,),
            ).fetchall()
        return [self._task(row) for row in rows]

    def claim(self, task_id: str, provider_id: str) -> AgentTask:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET status = 'running', provider_id = ?, updated_at = ?
                WHERE id = ? AND status = 'awaiting_approval'
                """,
                (provider_id, now, task_id),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT id, status FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise TaskNotFound("No se encontró la tarea.")
                raise TaskConflict("La tarea ya no está pendiente de aprobación.")
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._task(row)

    def complete(
        self, task_id: str, *, result: str | None = None, error: str | None = None
    ) -> AgentTask:
        now = datetime.now(timezone.utc).isoformat()
        status = "completed" if error is None else "failed"
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET status = ?, updated_at = ?, result = ?, error = ?
                WHERE id = ? AND status = 'running'
                """,
                (status, now, result, error, task_id),
            )
            if cursor.rowcount == 0:
                raise TaskConflict("No se pudo guardar el resultado de la tarea.")
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._task(row)


class AgentEngine:
    """Create reviewable tasks and execute only after explicit user approval."""

    def __init__(
        self,
        store: TaskStore | None = None,
        provider: AgentProvider | None = None,
        provider_status: ProviderStatus | None = None,
        provider_statuses: tuple[ProviderStatus, ...] = (),
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        self.store = store or TaskStore()
        self.provider = provider
        self.provider_status = provider_status
        self.provider_statuses = provider_statuses
        self.sensitive_values = tuple(value for value in sensitive_values if value)

    def create_task(self, instruction: str) -> AgentTask:
        if not isinstance(instruction, str) or not instruction.strip():
            raise TaskValidationError("Escribe una instrucción antes de crear la tarea.")
        instruction = instruction.strip()
        if len(instruction) > MAX_INSTRUCTION_LENGTH:
            raise TaskValidationError(
                f"La instrucción no puede superar {MAX_INSTRUCTION_LENGTH} caracteres."
            )
        if any(secret in instruction for secret in self.sensitive_values):
            raise TaskValidationError(
                "La instrucción contiene una credencial configurada. Elimínala antes de continuar."
            )
        provider_id = self.provider_status.identifier if self.provider_status else None
        return self.store.create(instruction, provider_id)

    def approve_task(self, task_id: str) -> AgentTask:
        if self.provider is None or self.provider_status is None or not self.provider_status.enabled:
            raise AgentUnavailable(
                "No hay un proveedor habilitado. Configura el proveedor y la autorización "
                "local antes de aprobar tareas."
            )
        task = self.store.claim(task_id, self.provider.identifier)
        try:
            result = self.provider.generate(task.instruction)
        except ProviderError as error:
            return self.store.complete(task.id, error=str(error))
        for secret in self.sensitive_values:
            result = result.replace(secret, "[API KEY REDACTED]")
        return self.store.complete(task.id, result=result)

    def task_history(self) -> list[AgentTask]:
        return self.store.list_recent()

    def public_status(self) -> dict[str, object]:
        status = self.provider_status
        return {
            "provider": (
                {
                    "id": status.identifier,
                    "name": status.display_name,
                    "configured": status.configured,
                    "enabled": status.enabled,
                    "simulation": status.simulation,
                    "message": status.message,
                }
                if status is not None
                else {
                    "id": None,
                    "name": "Ningún proveedor seleccionado",
                    "configured": False,
                    "enabled": False,
                    "simulation": False,
                    "message": "Configura un proveedor en el entorno del servidor.",
                }
            ),
            "providers": [
                {
                    "id": item.identifier,
                    "name": item.display_name,
                    "configured": item.configured,
                    "enabled": item.enabled,
                    "simulation": item.simulation,
                    "message": item.message,
                }
                for item in self.provider_statuses
            ],
        }


def create_agent_engine(database_path: str | Path | None = None) -> AgentEngine:
    """Load server-side provider configuration without exposing credentials."""
    from aibridge.providers import configure_providers

    configuration = configure_providers()
    return AgentEngine(
        store=TaskStore(database_path),
        provider=configuration.selected_provider,
        provider_status=configuration.selected_status,
        provider_statuses=configuration.statuses,
        sensitive_values=configured_api_keys(),
    )
