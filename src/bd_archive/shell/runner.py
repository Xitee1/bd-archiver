import signal
import subprocess


def _check_sigint(returncode: int) -> None:
    """If the child was killed by SIGINT, convert that into KeyboardInterrupt
    so the top-level handler emits a single uniform cancel message instead
    of a noisy CalledProcessError. Children share our process group by
    default, so a user Ctrl+C hits them too; this just normalises the
    bubble-up path.
    """
    if returncode == -signal.SIGINT:
        raise KeyboardInterrupt


def run(
    cmd: list[str], *, label: str = "", check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess:
    if capture:
        # check=False here so we can intercept the SIGINT case before
        # subprocess.run synthesises a CalledProcessError on its own.
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        _check_sigint(r.returncode)
        if check and r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
        return r

    prefix = f"  [{label}] " if label else "  "
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            print(f"{prefix}{line}", end="")
        proc.wait()
    except KeyboardInterrupt:
        # Child is in our process group → SIGINT already reached it.
        # Wait briefly for it to die; if it's stuck, escalate to SIGTERM
        # so we don't leak a zombie when we bubble up.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        raise
    _check_sigint(proc.returncode)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return subprocess.CompletedProcess(cmd, proc.returncode)
