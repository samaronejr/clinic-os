"""Test-only process placement for real performance scenario workers."""

from multiprocessing.context import SpawnProcess
from unittest.mock import patch


class UnconfinedProcess(SpawnProcess):
    """Keep spawned worker bodies real without requiring delegated cgroups."""

    def run(self) -> None:
        """Replace only the workers' process-placement calls."""
        with (
            patch("ops.perf.ai_load.attach"),
            patch("ops.perf.import_load.attach"),
        ):
            super().run()
