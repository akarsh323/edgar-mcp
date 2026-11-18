"""Inspect registry hook.

Listed under the `inspect_ai` entry-point group in pyproject.toml. Importing
it registers the `edgar_tools` task and the scripted-agent model provider.
"""

from edgar_mcp.evals.reference import ScriptedAgentAPI
from edgar_mcp.evals.task import edgar_tools

__all__ = ["ScriptedAgentAPI", "edgar_tools"]
