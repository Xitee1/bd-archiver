import signal
import subprocess
from collections.abc import Callable


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
    cmd: list[str],
    *,
    label: str = "",
    check: bool = True,
    capture: bool = False,
    passthrough: bool = False,
    output_transform: Callable[[str], str] | None = None,
) -> subprocess.CompletedProcess:
    if capture and passthrough:
        raise ValueError("capture and passthrough are mutually exclusive")
    if output_transform is not None and (capture or passthrough):
        raise ValueError("output_transform requires streaming output")

    if capture:
        # check=False here so we can intercept the SIGINT case before
        # subprocess.run synthesises a CalledProcessError on its own.
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        _check_sigint(r.returncode)
        if check and r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
        return r

    if passthrough:
        # Inherit our stdout/stderr so the child writes straight to the
        # user's terminal — required for tools whose progress uses \r
        # to repaint a single line (par2 "Scanning: X%"). The default
        # streaming path below reads until \n, which buffers those
        # updates and shows nothing live. Trade-off: no [label] prefix.
        r = subprocess.run(cmd, check=False)
        _check_sigint(r.returncode)
        if check and r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, cmd)
        return r

    prefix = f"  [{label}] " if label else "  "
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    if output_transform is not None:
        # Recognize both record separators while preserving untouched PAR2 output.
        proc.stdout.reconfigure(newline="")
    try:
        for line in proc.stdout:
            # Universal newlines also deliver carriage-return progress records.
            if output_transform is not None:
                print(output_transform(line), end="", flush=True)
            else:
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
    finally:
        proc.stdout.close()
    _check_sigint(proc.returncode)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return subprocess.CompletedProcess(cmd, proc.returncode)
