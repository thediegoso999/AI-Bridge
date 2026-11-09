"""Private, read-only HTTP control center for Codespaces and local use."""

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit

from aibridge.github import GitHubClient, GitHubUnavailable
from aibridge.inspection import inspect_repository

MAX_PREVIEW_BYTES = 160_000
EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".ssh",
}
EXCLUDED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".p8"}


class DashboardHandler(BaseHTTPRequestHandler):
    """Serve the dashboard and read-only JSON endpoints."""

    repository_root = Path.cwd()
    github_client = GitHubClient()
    web_root = Path(__file__).with_name("web")

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        if request.path == "/":
            self._send_static("index.html", "text/html; charset=utf-8")
            return
        if request.path == "/assets/app.js":
            self._send_static("app.js", "text/javascript; charset=utf-8")
            return
        if request.path == "/assets/styles.css":
            self._send_static("styles.css", "text/css; charset=utf-8")
            return
        if not request.path.startswith("/api/"):
            self._send_json(404, {"error": "Ruta no encontrada."})
            return

        query = parse_qs(request.query, keep_blank_values=True)
        try:
            payload = self._api_response(request.path, query)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except GitHubUnavailable as error:
            self._send_json(503, {"error": str(error)})
        except FileNotFoundError:
            self._send_json(404, {"error": "La ruta solicitada no existe."})
        except PermissionError:
            self._send_json(403, {"error": "No se permite acceder a esa ruta."})
        except OSError:
            self._send_json(500, {"error": "No se pudo leer el recurso local."})
        else:
            self._send_json(200, payload)

    def _api_response(self, route: str, query: dict[str, list[str]]) -> object:
        if route == "/api/status":
            snapshot = inspect_repository(self.repository_root)
            return {
                "application": "online",
                "repository": asdict(snapshot),
                "github": self.github_client.connection_status(),
                "agents": {
                    "configured": False,
                    "message": "No hay un proveedor de agentes configurado.",
                },
            }
        if route == "/api/repositories":
            return {"repositories": self.github_client.repositories()}
        if route == "/api/actions":
            return {"runs": self.github_client.workflow_runs(self._query_value(query, "repo"))}
        if route == "/api/action-logs":
            return {
                "logs": self.github_client.workflow_logs(
                    self._query_value(query, "repo"),
                    self._query_value(query, "run_id"),
                )
            }
        if route == "/api/files":
            return self._list_files(self._query_value(query, "path", default="."))
        if route == "/api/file":
            return self._read_file(self._query_value(query, "path"))
        raise ValueError("Ruta API no reconocida.")

    @staticmethod
    def _query_value(
        query: dict[str, list[str]], key: str, default: str | None = None
    ) -> str:
        values = query.get(key)
        value = values[0] if values else default
        if value is None or value == "":
            raise ValueError(f"Falta el parámetro {key}.")
        return value

    def _safe_path(self, relative_path: str) -> Path:
        if "\\" in relative_path:
            raise ValueError("La ruta no es válida.")
        requested = PurePosixPath(relative_path)
        if requested.is_absolute() or ".." in requested.parts:
            raise ValueError("La ruta debe permanecer dentro del repositorio.")

        candidate = self.repository_root
        for part in requested.parts:
            if part != ".":
                candidate = candidate / part
                if candidate.is_symlink():
                    raise PermissionError("No se permite seguir enlaces simbólicos.")

        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(self.repository_root):
            raise PermissionError("La ruta debe permanecer dentro del repositorio.")
        if self._is_excluded(resolved.name):
            raise PermissionError("No se permite acceder a este tipo de archivo.")
        return resolved

    def _list_files(self, relative_path: str) -> dict[str, object]:
        directory = self._safe_path(relative_path)
        if not directory.is_dir():
            raise ValueError("La ruta solicitada no es un directorio.")

        entries = []
        for path in directory.iterdir():
            if path.is_symlink() or self._is_excluded(path.name):
                continue
            entries.append(
                {
                    "name": path.name,
                    "path": path.relative_to(self.repository_root).as_posix(),
                    "kind": "directory" if path.is_dir() else "file",
                    "size": path.stat().st_size if path.is_file() else None,
                }
            )
        entries.sort(key=lambda entry: (entry["kind"] != "directory", entry["name"].lower()))
        return {"path": relative_path, "entries": entries}

    def _read_file(self, relative_path: str) -> dict[str, str]:
        file_path = self._safe_path(relative_path)
        if not file_path.is_file():
            raise ValueError("La ruta solicitada no es un archivo.")
        if file_path.stat().st_size > MAX_PREVIEW_BYTES:
            raise ValueError("El archivo supera el límite de vista previa (160 KB).")

        content = file_path.read_bytes()
        if b"\0" in content:
            raise ValueError("No se puede previsualizar un archivo binario.")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("El archivo no contiene texto UTF-8 válido.") from error
        return {"path": file_path.relative_to(self.repository_root).as_posix(), "content": text}

    @staticmethod
    def _is_excluded(name: str) -> bool:
        lowered = name.lower()
        return (
            name in EXCLUDED_DIRECTORIES
            or lowered == ".env"
            or lowered.startswith(".env.")
            or lowered in {"id_rsa", "id_ed25519"}
            or Path(lowered).suffix in EXCLUDED_SUFFIXES
        )

    def _send_static(self, filename: str, content_type: str) -> None:
        try:
            content = (self.web_root / filename).read_bytes()
        except OSError:
            self._send_json(404, {"error": "Recurso de interfaz no encontrado."})
            return
        self._send(200, content, content_type)

    def _send_json(self, status: int, payload: object) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, content, "application/json; charset=utf-8")

    def _send(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; style-src 'self'; "
            "script-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(content)

def create_server(
    root: str | Path = ".",
    host: str = "127.0.0.1",
    port: int = 8000,
    github_client: GitHubClient | None = None,
) -> ThreadingHTTPServer:
    """Create a dashboard server bound to a specific local repository."""
    repository_root = Path(root).expanduser().resolve()
    if not repository_root.is_dir():
        raise ValueError(f"Not a directory: {repository_root}")

    handler = type(
        "RepositoryDashboardHandler",
        (DashboardHandler,),
        {
            "repository_root": repository_root,
            "github_client": github_client or GitHubClient(),
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server