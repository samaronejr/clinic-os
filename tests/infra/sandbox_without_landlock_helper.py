"""Run the sandbox launcher on a kernel that cannot apply Landlock."""

import importlib.util
import sys


class _Gone:
    @staticmethod
    def abi() -> int:
        message = "landlock_unavailable: ENOSYS"
        raise OSError(38, message)


spec = importlib.util.spec_from_file_location("sandbox", sys.argv[1])
if spec is None or spec.loader is None:
    message = "sandbox launcher is not loadable"
    raise SystemExit(message)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
vars(module)["_load_landlock"] = lambda: _Gone
vars(module)["main"](sys.argv[2:])
