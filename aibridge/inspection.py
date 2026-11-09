"""Read-only inspection of a local project repository."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepositorySnapshot:
    """A small, serializable summary of a repository's development setup."""

    name: str
    path: str
    has_readme: bool
    has_devcontainer: bool
    has_python_project: bool
    workflow_files: tuple[str, ...]
    test_files: tuple[str, ...]


def inspect_repository(root: str | Path = ".") -> RepositorySnapshot:
    """Inspect repository conventions without changing files or using the network."""
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise ValueError(f"Not a directory: {root_path}")

    workflows_path = root_path / ".github" / "workflows"
    workflow_files = ()
    if workflows_path.is_dir():
        workflow_files = tuple(
            sorted(
                str(path.relative_to(root_path))
                for path in workflows_path.iterdir()
                if path.is_file() and path.suffix.lower() in {".yml", ".yaml"}
            )
        )

    tests_path = root_path / "tests"
    test_files = ()
    if tests_path.is_dir():
        test_files = tuple(
            sorted(
                str(path.relative_to(root_path))
                for path in tests_path.rglob("test*.py")
                if path.is_file()
            )
        )

    return RepositorySnapshot(
        name=root_path.name,
        path=str(root_path),
        has_readme=any(
            path.is_file() and path.name.lower().startswith("readme")
            for path in root_path.iterdir()
        ),
        has_devcontainer=(root_path / ".devcontainer" / "devcontainer.json").is_file(),
        has_python_project=any(
            (root_path / filename).is_file()
            for filename in ("pyproject.toml", "setup.py", "setup.cfg")
        ),
        workflow_files=workflow_files,
        test_files=test_files,
    )