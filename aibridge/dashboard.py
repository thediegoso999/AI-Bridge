"""Private HTTP control center with a read-only workspace and approved agent tasks."""

import json
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit

from aibridge.agents import (
    AgentEngine,
    AgentUnavailable,
    TaskConflict,
    TaskNotFound,
    TaskValidationError,
)
from aibridge.github import GitHubClient, GitHubUnavailable
from aibridge.inspection import inspect_repository
from aibridge.router import classify_task
from aibridge.smart_router import SmartTaskEngine, create_smart_engine

MAX_PREVIEW_BYTES = 160_000
MAX_AGENT_REQUEST_BYTES = 64_000
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
    """Serve the dashboard and authenticated, approval-gated agent endpoints."""

    repository_root = Path.cwd()
    github_client = GitHubClient()
    agent_engine: AgentEngine | SmartTaskEngine | None = None
    agent_engine_lock = threading.Lock()
    authorization_token = ""
    allowed_origins: frozenset[str] = frozenset()
    web_root = Path(__file__).with_name("web")

    def do_GET(self) -> None:
        if not self._valid_host():
            self._send_json(403, {"error": "Host no autorizado."})
            return
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
        if request.path == "/api/agents" and not self._is_authenticated():
            self._send_json(401, {"error": "Inicia sesión para consultar el historial de tareas."})
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
        except sqlite3.Error:
            self._send_json(500, {"error": "No se pudo acceder al historial local de tareas."})
        except OSError:
            self._send_json(500, {"error": "No se pudo leer el recurso local."})
        else:
            self._send_json(200, payload)

    def do_POST(self) -> None:
        if not self._valid_same_origin_request():
            self._send_json(403, {"error": "Solicitud no autorizada o token CSRF no válido."})
            return
        if self.path not in {
            "/api/agents/auth",
            "/api/agents/tasks",
            "/api/agents/router",
            "/api/agents/plan",
        } and not re.fullmatch(
            r"/api/agents/tasks/[0-9a-f]{32}/approve", self.path
        ):
            self._send_json(404, {"error": "Ruta no encontrada."})
            return

        if self.path != "/api/agents/auth" and not self._is_authenticated():
            self._send_json(401, {"error": "Inicia sesión para administrar tareas."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Longitud de solicitud no válida."})
            return
        if content_length < 0 or content_length > MAX_AGENT_REQUEST_BYTES:
            self._send_json(413, {"error": "La solicitud supera el tamaño permitido."})
            return

        try:
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("El contenido debe enviarse como JSON.")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("El cuerpo JSON debe ser un objeto.")

            if self.path == "/api/agents/auth":
                access_code = payload.get("access_code")
                if not isinstance(access_code, str) or not secrets.compare_digest(
                    access_code, self.authorization_token
                ):
                    self._send_json(401, {"error": "Código de acceso no válido."})
                    return
                self._auth_cookie = self._create_session_token()
                self._send_json(200, {"authenticated": True})
                return

            if self.path == "/api/agents/tasks":
                task = self._get_agent_engine().create_task(payload.get("instruction"))
                self._send_json(201, {"task": asdict(task)})
                return

            if self.path == "/api/agents/router":
                engine = self._get_agent_engine()
                if not isinstance(engine, SmartTaskEngine):
                    raise ValueError("El router inteligente no está disponible.")
                settings = engine.update_router_settings(payload)
                self._send_json(200, {"settings": settings.as_dict()})
                return

            if self.path == "/api/agents/plan":
                engine = self._get_agent_engine()
                if not isinstance(engine, SmartTaskEngine):
                    raise ValueError("El router inteligente no está disponible.")
                instruction = payload.get("instruction")
                if not isinstance(instruction, str) or not instruction.strip():
                    raise TaskValidationError("Escribe una instrucción antes de consultar la ruta.")
                if len(instruction) > 8_000:
                    raise TaskValidationError("La instrucción no puede superar 8000 caracteres.")
                plan = engine.route_plan(instruction.strip())
                self._send_json(
                    200,
                    {
                        "task_type": classify_task(instruction.strip()),
                        "candidates": [asdict(candidate) for candidate in plan],
                    },
                )
                return

            match = re.fullmatch(r"/api/agents/tasks/([0-9a-f]{32})/approve", self.path)
            if match is None:
                self._send_json(404, {"error": "Ruta no encontrada."})
                return
            engine = self._get_agent_engine()
            if isinstance(engine, SmartTaskEngine):
                provider_ids = payload.get("provider_ids")
                if not isinstance(provider_ids, list) or any(
                    not isinstance(provider_id, str) for provider_id in provider_ids
                ):
                    raise TaskValidationError(
                        "Selecciona los proveedores permitidos antes de aprobar."
                    )
                task = engine.approve_task(
                    match.group(1),
                    provider_ids=provider_ids,
                    external_consent=payload.get("external_consent") is True,
                )
                engine.start_worker()
                self._send_json(202, {"task": asdict(task)})
                return
            task = engine.approve_task(match.group(1))
            self._send_json(200, {"task": asdict(task)})
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            self._send_json(400, {"error": "El cuerpo de la solicitud no es JSON válido."})
        except TaskValidationError as error:
            self._send_json(400, {"error": str(error)})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except AgentUnavailable as error:
            self._send_json(503, {"error": str(error)})
        except TaskNotFound as error:
            self._send_json(404, {"error": str(error)})
        except TaskConflict as error:
            self._send_json(409, {"error": str(error)})
        except sqlite3.Error:
            self._send_json(500, {"error": "No se pudo acceder al historial local de tareas."})

    def _get_agent_engine(self) -> AgentEngine | SmartTaskEngine:
        handler_class = type(self)
        with handler_class.agent_engine_lock:
            if handler_class.agent_engine is None:
                handler_class.agent_engine = create_smart_engine()
            return handler_class.agent_engine

    def _agent_response(self) -> dict[str, object]:
        engine = self._get_agent_engine()
        return {
            **engine.public_status(),
            "tasks": [asdict(task) for task in engine.task_history()],
        }

    def _session_response(self) -> dict[str, object]:
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            cookies = SimpleCookie()
        cookie = cookies.get("aibridge_csrf")
        token = (
            cookie.value
            if cookie and re.fullmatch(r"[A-Za-z0-9_-]{40,}", cookie.value)
            else secrets.token_urlsafe(32)
        )
        self._csrf_cookie = token
        return {"csrf_token": token, "authenticated": self._is_authenticated()}

    def _valid_host(self) -> bool:
        host = self.headers.get("Host", "").lower()
        allowed_hosts = {
            urlsplit(origin).netloc.lower() for origin in self.allowed_origins
        }
        return bool(host and host in allowed_hosts)

    def _valid_same_origin_request(self) -> bool:
        origin = urlsplit(self.headers.get("Origin", ""))
        host = self.headers.get("Host", "")
        origin_value = f"{origin.scheme}://{origin.netloc}".lower()
        if (
            origin.scheme not in {"http", "https"}
            or origin.path
            or origin.query
            or origin.fragment
            or origin_value not in self.allowed_origins
            or origin.netloc.lower() != host.lower()
        ):
            return False

        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        cookie = cookies.get("aibridge_csrf")
        header_token = self.headers.get("X-CSRF-Token", "")
        return bool(
            cookie
            and re.fullmatch(r"[A-Za-z0-9_-]{40,}", cookie.value)
            and secrets.compare_digest(cookie.value, header_token)
        )

    def _is_authenticated(self) -> bool:
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        cookie = cookies.get("aibridge_session")
        if cookie is None:
            return False
        try:
            timestamp, nonce, signature = cookie.value.split(".", 2)
            issued_at = int(timestamp)
        except (ValueError, TypeError):
            return False
        now = int(time.time())
        if issued_at > now or now - issued_at > 8 * 60 * 60:
            return False
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,}", nonce) or not re.fullmatch(
            r"[0-9a-f]{64}", signature
        ):
            return False
        expected = hmac.new(
            self.authorization_token.encode("utf-8"),
            f"{timestamp}.{nonce}".encode("ascii"),
            "sha256",
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    def _create_session_token(self) -> str:
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(32)
        signature = hmac.new(
            self.authorization_token.encode("utf-8"),
            f"{timestamp}.{nonce}".encode("ascii"),
            "sha256",
        ).hexdigest()
        return f"{timestamp}.{nonce}.{signature}"

    def _api_response(self, route: str, query: dict[str, list[str]]) -> object:
        if route == "/api/status":
            snapshot = inspect_repository(self.repository_root)
            provider = self._get_agent_engine().public_status()["provider"]
            return {
                "application": "online",
                "repository": asdict(snapshot),
                "github": self.github_client.connection_status(),
                "agents": provider,
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
        if route == "/api/agents":
            return self._agent_response()
        if route == "/api/session":
            return self._session_response()
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
        csrf_cookie = getattr(self, "_csrf_cookie", None)
        auth_cookie = getattr(self, "_auth_cookie", None)
        secure = (
            self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
            == "https"
        )
        cookie_attributes = "; HttpOnly; SameSite=Strict; Path=/"
        if secure:
            cookie_attributes += "; Secure"
        if csrf_cookie:
            self.send_header("Set-Cookie", f"aibridge_csrf={csrf_cookie}{cookie_attributes}")
        if auth_cookie:
            self.send_header(
                "Set-Cookie",
                f"aibridge_session={auth_cookie}{cookie_attributes}; Max-Age=28800",
            )
        self.end_headers()
        self.wfile.write(content)

class DashboardHTTPServer(ThreadingHTTPServer):
    def server_close(self) -> None:
        engine = self.RequestHandlerClass.agent_engine
        if isinstance(engine, SmartTaskEngine):
            engine.stop_worker()
        super().server_close()


def create_server(
    root: str | Path = ".",
    host: str = "127.0.0.1",
    port: int = 8000,
    github_client: GitHubClient | None = None,
    agent_engine: AgentEngine | SmartTaskEngine | None = None,
    authorization_token: str | None = None,
    allowed_origins: frozenset[str] | None = None,
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
            "agent_engine": agent_engine,
            "agent_engine_lock": threading.Lock(),
            "authorization_token": authorization_token or secrets.token_urlsafe(32),
            "allowed_origins": frozenset(),
        },
    )
    server = DashboardHTTPServer((host, port), handler)
    try:
        permitted_origins = _dashboard_origins(host, server.server_port)
        origins = (
            permitted_origins
            if allowed_origins is None
            else frozenset(origin.lower() for origin in allowed_origins)
        )
        if not origins:
            raise ValueError("No hay orígenes permitidos configurados para el dashboard.")
        if not origins.issubset(permitted_origins):
            raise ValueError("Solo se permiten orígenes locales y del Codespace actual.")
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or "*" in parsed.netloc
            ):
                raise ValueError("La lista de orígenes permitidos contiene un valor no válido.")
        handler.allowed_origins = origins
    except ValueError:
        server.server_close()
        raise
    server.daemon_threads = True
    server.authorization_token = handler.authorization_token
    return server


def _dashboard_origins(host: str, port: int) -> frozenset[str]:
    origins = {
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
        f"http://[::1]:{port}",
    }
    if os.environ.get("CODESPACES", "").lower() == "true":
        codespace_name = os.environ.get("CODESPACE_NAME", "").strip()
        forwarding_domain = os.environ.get(
            "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", ""
        ).strip()
        if not _is_dns_name(codespace_name, allow_subdomains=False):
            raise ValueError("CODESPACE_NAME no contiene un nombre válido.")
        if not _is_dns_name(forwarding_domain):
            raise ValueError(
                "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN no contiene un dominio válido."
            )
        origins.add(
            f"https://{codespace_name.lower()}-{port}.{forwarding_domain.lower()}"
        )
    elif host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "El dashboard solo puede escuchar en loopback o en un Codespace."
        )
    if host in {"127.0.0.1", "localhost", "::1"}:
        hostname = f"[{host}]" if ":" in host else host
        origins.add(f"http://{hostname}:{port}")
    return frozenset(origins)


def _is_dns_name(value: str, *, allow_subdomains: bool = True) -> bool:
    if not value or len(value) > 253:
        return False
    labels = value.lower().split(".")
    if not allow_subdomains and len(labels) != 1:
        return False
    return all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        is not None
        for label in labels
    )