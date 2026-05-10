import subprocess


def run(cmd: list[str], *, label: str = "", check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, check=check)

    prefix = f"  [{label}] " if label else "  "
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        print(f"{prefix}{line}", end="")
    proc.wait()
    r = subprocess.CompletedProcess(cmd, proc.returncode)
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, cmd)
    return r
