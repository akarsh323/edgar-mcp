"""Runtime settings, read from environment variables.

* `EDGAR_MCP_USER_AGENT`: required for live requests. The SEC asks automated
  clients to identify themselves, e.g. `"Jane Analyst jane@example.org"`.
* `EDGAR_MCP_SNAPSHOT`: serve a recorded snapshot instead of calling the
  SEC. A directory, or `bundled` for the snapshot shipped with the package.
* `EDGAR_MCP_TOOLSET`: `designed` (default) or `baseline`. The baseline
  exists to be measured against.
* `EDGAR_MCP_MAX_REQUESTS_PER_SECOND`: live request rate. Default 5; the
  SEC's published ceiling is 10.
"""

from __future__ import annotations

import dataclasses
import enum
import os
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

ENV_USER_AGENT = "EDGAR_MCP_USER_AGENT"
ENV_SNAPSHOT = "EDGAR_MCP_SNAPSHOT"
ENV_TOOLSET = "EDGAR_MCP_TOOLSET"
ENV_RATE = "EDGAR_MCP_MAX_REQUESTS_PER_SECOND"

BUNDLED = "bundled"
SEC_RATE_CEILING = 10.0
DEFAULT_RATE = 5.0


class Toolset(enum.StrEnum):
    """Which tool surface the server exposes."""

    DESIGNED = "designed"
    """Task-shaped tools with filtering, paging and actionable errors."""

    BASELINE = "baseline"
    """Thin wrappers over the raw SEC endpoints, kept for comparison."""


def bundled_snapshot() -> Path:
    """Returns the directory of the snapshot shipped with the package."""
    return Path(str(resources.files("edgar_mcp.data").joinpath("snapshot")))


@dataclasses.dataclass(frozen=True)
class Settings:
    """Server settings.

    Attributes:
      user_agent: Identification sent to the SEC on live requests.
      snapshot_dir: Directory to serve responses from, or `None` for live.
      toolset: Tool surface to expose.
      max_requests_per_second: Live request rate limit.
    """

    user_agent: str | None = None
    snapshot_dir: Path | None = None
    toolset: Toolset = Toolset.DESIGNED
    max_requests_per_second: float = DEFAULT_RATE

    @property
    def offline(self) -> bool:
        """Whether responses come from a snapshot rather than the network."""
        return self.snapshot_dir is not None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Builds settings from environment variables.

        Args:
          environ: Variables to read. Defaults to `os.environ`.

        Returns:
          The settings.

        Raises:
          ValueError: If a variable has an invalid value.
        """
        env = os.environ if environ is None else environ
        snapshot = env.get(ENV_SNAPSHOT, "").strip()
        snapshot_dir = None
        if snapshot == BUNDLED:
            snapshot_dir = bundled_snapshot()
        elif snapshot:
            snapshot_dir = Path(snapshot).expanduser()
        if snapshot_dir is not None and not snapshot_dir.is_dir():
            raise ValueError(f"{ENV_SNAPSHOT} is not a directory: {snapshot}")

        rate = float(env.get(ENV_RATE, DEFAULT_RATE))
        if not 0 < rate <= SEC_RATE_CEILING:
            raise ValueError(
                f"{ENV_RATE} must be in (0, {SEC_RATE_CEILING:g}], got {rate}"
            )
        return cls(
            user_agent=env.get(ENV_USER_AGENT, "").strip() or None,
            snapshot_dir=snapshot_dir,
            toolset=Toolset(env.get(ENV_TOOLSET, Toolset.DESIGNED.value)),
            max_requests_per_second=rate,
        )
