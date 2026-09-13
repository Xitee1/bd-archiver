"""Build the Linux preload helper for both editable installs and wheels."""

import os
import shlex
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel":
            return
        if sys.platform != "linux":
            raise RuntimeError("bd-archive's burn timeout helper requires Linux")
        native = Path(self.root) / "src/bd_archive/_native"
        destination = native / "burn_timeout.so"
        with tempfile.TemporaryDirectory(prefix="bd-timeout-build-") as temporary:
            output = Path(temporary) / destination.name
            subprocess.run(
                [
                    *shlex.split(os.environ.get("CC", "cc")),
                    "-std=c11",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-fPIC",
                    "-shared",
                    "-Wl,-z,relro,-z,now",
                    "-Wl,-Bsymbolic-functions",
                    str(native / "burn_timeout.c"),
                    "-o",
                    str(output),
                    "-ldl",
                ],
                check=True,
            )
            # Replace rather than truncate a library mapped by a running burn.
            pending = destination.with_suffix(".so.pending")
            pending.write_bytes(output.read_bytes())
            pending.chmod(0o755)
            pending.replace(destination)
        build_data["artifacts"].append("src/bd_archive/_native/*.so")
        build_data["pure_python"] = False
        # No Python ABI is used. Do not advertise cross-libc portability.
        platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
        build_data["tag"] = f"py3-none-{platform}"
