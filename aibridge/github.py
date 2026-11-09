"""Read-only GitHub access through the user's authenticated GitHub CLI."""

import json
import re
import subprocess


class GitHubUnavailable(RuntimeError):
    """Raised when GitHub CLI access or the requested data is unavailable."""


class GitHubClient:
    """Read GitHub data without handling or returning authentication tokens."""

    def __init__(self, timeout: int = 20, log_limit: int = 120_000) -> None:
        self.timeout = timeout
        self.log_limit = log_limit

    def _run(self, *arguments: str) -> str:
        try:
            result = subprocess.run(
                ["gh", *arguments],
                capture_output=True,
                check=False,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as error:
            raise GitHubUnavailable(
                "GitHub CLI (gh) no está instalado en este entorno."
            ) from error
        except subprocess.TimeoutExpired as error:
            raise GitHubUnavailable("GitHub CLI agotó el tiempo de espera.") from error

        if result.returncode != 0:
            raise GitHubUnavailable(
                "GitHub CLI no pudo completar la consulta. Comprueba "
                "gh auth status y que la cuenta tenga permisos de lectura."
            )
        return result.stdout

    def _get_json(self, endpoint: str) -> object:
        try:
            return json.loads(self._run("api", endpoint))
        except json.JSONDecodeError as error:
            raise GitHubUnavailable("GitHub devolvió una respuesta no válida.") from error

    def connection_status(self) -> dict[str, object]:
        try:
            self._run("auth", "status", "--hostname", "github.com")
            viewer = self._get_json("user")
        except GitHubUnavailable as error:
            return {"connected": False, "message": str(error)}

        if not isinstance(viewer, dict) or not isinstance(viewer.get("login"), str):
            return {
                "connected": False,
                "message": "No se pudo identificar la cuenta autenticada.",
            }
        return {"connected": True, "login": viewer["login"]}

    def repositories(self) -> list[dict[str, object]]:
        response = self._get_json(
            "user/repos?sort=updated&per_page=30&affiliation=owner,collaborator,organization_member"
        )
        if not isinstance(response, list):
            raise GitHubUnavailable("GitHub devolvió una lista de repositorios no válida.")

        repositories = []
        for repository in response:
            if not isinstance(repository, dict) or not isinstance(
                repository.get("full_name"), str
            ):
                continue
            repositories.append(
                {
                    "full_name": repository["full_name"],
                    "private": bool(repository.get("private")),
                    "default_branch": repository.get("default_branch"),
                    "updated_at": repository.get("updated_at"),
                }
            )
        return repositories

    def workflow_runs(self, repository: str) -> list[dict[str, object]]:
        self._validate_repository(repository)
        response = self._get_json(
            f"repos/{repository}/actions/runs?per_page=20"
        )
        if not isinstance(response, dict) or not isinstance(
            response.get("workflow_runs"), list
        ):
            raise GitHubUnavailable("GitHub devolvió una respuesta de Actions no válida.")

        fields = (
            "id",
            "name",
            "display_title",
            "event",
            "status",
            "conclusion",
            "created_at",
            "updated_at",
            "head_branch",
            "run_number",
        )
        return [
            {field: run.get(field) for field in fields}
            for run in response["workflow_runs"]
            if isinstance(run, dict)
        ]

    def workflow_logs(self, repository: str, run_id: str) -> str:
        self._validate_repository(repository)
        if not run_id.isdecimal():
            raise ValueError("El identificador de ejecución debe ser numérico.")
        output = self._run("run", "view", run_id, "--repo", repository, "--log")
        if len(output) > self.log_limit:
            return output[: self.log_limit] + "\n\n[Registro truncado por tamaño.]"
        return output

    @staticmethod
    def _validate_repository(repository: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("El repositorio debe tener el formato propietario/repositorio.")