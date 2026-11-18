"""Tests that packaging metadata and the bundled data stay consistent."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import edgar_mcp
from edgar_mcp.config import bundled_snapshot
from edgar_mcp.evals import _registry

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def requirements(name):
    lines = (ROOT / name).read_text().splitlines()
    return {
        line.strip()
        for line in lines
        if line.strip() and not line.startswith(("#", "-r"))
    }


def test_requirements_match_pyproject():
    project = PYPROJECT["project"]
    extras = project["optional-dependencies"]
    assert requirements("requirements.txt") == set(project["dependencies"])
    assert requirements("requirements-dev.txt") == set(extras["eval"]) | set(
        extras["dev"]
    )


def test_version_entry_points_and_scripts():
    project = PYPROJECT["project"]
    assert project["version"] == edgar_mcp.__version__
    assert project["entry-points"]["inspect_ai"] == {
        "edgar_mcp": "edgar_mcp.evals._registry"
    }
    assert set(_registry.__all__) == {"ScriptedAgentAPI", "edgar_tools"}
    assert set(project["scripts"]) == {"edgar-mcp", "edgar-mcp-eval"}


def test_snapshot_manifest_lists_what_is_bundled():
    root = bundled_snapshot()
    manifest = json.loads((root / "manifest.json").read_text())
    for company in manifest["companies"]:
        path = (
            root / f"data.sec.gov/submissions/CIK{company['cik']:010d}.json.gz"
        )
        assert path.is_file()
    assert "user_agent" not in json.dumps(manifest).lower()


def test_cli_version():
    result = subprocess.run(
        [sys.executable, "-m", "edgar_mcp", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"edgar-mcp {edgar_mcp.__version__}"
