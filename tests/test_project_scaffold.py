import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_and_development_dependencies_are_exactly_pinned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    development_dependencies = project["project"]["optional-dependencies"]["dev"]

    assert dependencies
    assert development_dependencies
    assert all("==" in requirement for requirement in dependencies)
    assert all("==" in requirement for requirement in development_dependencies)


def test_live_foundry_gate_is_marked_for_ci_exclusion() -> None:
    pytest_options = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["tool"]["pytest"]["ini_options"]

    assert "not live" in pytest_options["addopts"]
    assert any(marker.startswith("live:") for marker in pytest_options["markers"])
