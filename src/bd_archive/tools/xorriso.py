import signal
import subprocess
import time
from pathlib import Path

# Window during which a second Ctrl+C is treated as a confirmed force-abort.
# 5s is long enough for a deliberate double-press but short enough that an
# accidental Ctrl+C plus a later real one don't compound.
BURN_ABORT_GRACE_S = 5.0

# Substrings libburn/xorriso emit when it cannot take exclusive control of
# the drive (held by MakeMKV, K3b, a desktop auto-mount probe, etc.). Matched
# case-insensitively against each output line. "aquire" is libburn's older
# misspelled symbol — kept so we still catch it on pre-1.5 builds.
_BUSY_MARKERS = (
    "cannot open busy device",
    "device or resource busy",
    "cannot acquire drive",
    "cannot aquire drive",
)


class DeviceBusyError(Exception):
    """xorriso/libburn couldn't take exclusive control of the drive —
    typically held by a tool like MakeMKV, K3b, or a desktop auto-mount
    probe."""

    def __init__(self, device: str):
        super().__init__(device)
        self.device = device


def burn(device: str, iso_path: Path, speed: str | None = None):
    """Burn a pre-built ISO file via xorriso (cdrecord emulation).

    `xorriso -as cdrecord ... <iso>` streams the ISO file's bytes
    verbatim into a single data track — no on-the-fly mkisofs, no
    re-mastering, so what's in the ISO file is exactly what ends up on
    the disc. Volume label, publisher, file layout are all already in
    the file from the build step. This replaces the abandoned growisofs
    (dvd+rw-tools, last release 2008), which auto-formats blank BD-R
    before writing and fails to even START the burn on some drive+media
    combos (the format step is rejected). xorriso does not format BD-R,
    so it sidesteps that failure entirely.

    Flags:
    -as cdrecord: cdrecord-compatible interface; the trailing ISO path
      is the track source written flatly to the medium (= growisofs's
      `-Z dev=image.iso`).
    -v: one verbosity level — xorriso is otherwise nearly silent. Output
      is newline-terminated (no `\\r` repaint), so the default streaming
      runner shows it fine.
    -multi: keep the BD-R appendable (do NOT close/finalize the disc).
      This matches the previous growisofs behaviour: growisofs's
      `-dvd-compat` only ever closed write-once DVD media (DVD-R/DVD+R) —
      it never closed BD-R, so these archive discs were always left
      appendable. We preserve that. Without `-multi`, xorriso WOULD close
      the BD-R, which would be a silent change of on-disc state; we don't
      want that. The disc still carries exactly one session (the
      pre-built ISO) — appendable just means the medium isn't sealed.
    stream_recording=on: prefer the requested write speed over the
      drive's write-error management, bringing BD effective speed near
      the media's nominal rate. Its only cost — disabling automatic
      replacement blocks on write errors — does not apply to our
      unformatted BD-R (no spare area exists to replace from), and
      integrity is covered by par2 FEC + sha512 + the post-burn verify.
      Kept because it is part of the known-good manual command on the
      finicky drive that motivated the growisofs→xorriso switch.
    -eject: eject the tray when done. growisofs did this implicitly; we
      ask for it explicitly because xorriso does NOT eject by default and
      `DiscIO.wait_for_disc_ready` relies on the eject to invalidate the
      kernel's cached "Blank BD-R" view of the medium (without that
      media-change event, mount keeps seeing the pre-burn blank state and
      udisks2 reports the disc as not-mountable).

    NOT passed, deliberately:
    blank=/format_*: xorriso does NOT format a blank BD-R by default, so
      defect management stays off, the Outer Spare Area is not reserved,
      and the full nominal Free Blocks capacity is writable at full
      speed — exactly what growisofs's `-use-the-force-luke=spare=none`
      bought us. We have par2 FEC + sha512 + a post-burn verify pass, so
      drive-firmware defect management is redundant.

      ⚠️ COUPLED to `tools.mediainfo.detect_disc_capacity`, which returns
      `Free Blocks` directly (nominal capacity). NEVER pass a `blank=` or
      format command here: xorriso would then reserve an Outer Spare Area
      (~256 MiB on 25 GB SL), writes past that LBA would fail, and Free
      Blocks would over-report the writable extent. To re-enable defect
      management you MUST also revert detect_disc_capacity to read the
      MMC-6 32h format-type descriptor (see commit 43fce62).
    -dao: do NOT add. The man page warns it "might prevent the write run"
      and it has no purpose on BD-R (no TAO/DAO distinction there).

    Ctrl+C during a burn would coaster a BD-R, so we trap SIGINT here:
    first press warns; a second press within BURN_ABORT_GRACE_S
    terminates xorriso and raises KeyboardInterrupt. xorriso runs in its
    own session (start_new_session=True) so it does NOT get SIGINT from
    the user's tty — only we decide when it dies.

    Raises DeviceBusyError if libburn reports it cannot take the drive;
    CalledProcessError on any other non-zero exit; KeyboardInterrupt if
    the user confirmed a mid-burn abort.
    """
    cmd = [
        "xorriso",
        "-as",
        "cdrecord",
        "-v",
        f"dev={device}",
        "-multi",
        "stream_recording=on",
        "-eject",
    ]
    if speed:
        cmd += [f"speed={speed}"]
    cmd += [str(iso_path)]

    # start_new_session=True isolates xorriso from the user's SIGINT so the
    # burn only dies when WE call terminate() — see handler below.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    state = {"first_press_at": None, "aborted": False}

    def handler(_signum, _frame):
        now = time.monotonic()
        prev = state["first_press_at"]
        if prev is not None and now - prev <= BURN_ABORT_GRACE_S:
            # Confirmed force-abort.
            print()
            print(
                "  [burn] Aborting xorriso — this disc will be unusable.",
                flush=True,
            )
            state["aborted"] = True
            proc.terminate()
            return
        state["first_press_at"] = now
        print()
        print(
            "  [burn] Burn in progress — Ctrl+C ignored to protect the disc.",
            flush=True,
        )
        print(
            f"  [burn] Press Ctrl+C again within {int(BURN_ABORT_GRACE_S)}s "
            f"to force-abort and waste this disc.",
            flush=True,
        )

    prev_handler = signal.signal(signal.SIGINT, handler)
    try:
        assert proc.stdout is not None
        device_busy = False
        for line in proc.stdout:
            print(f"  [burn] {line}", end="")
            low = line.lower()
            if any(marker in low for marker in _BUSY_MARKERS):
                device_busy = True
        proc.wait()
    finally:
        signal.signal(signal.SIGINT, prev_handler)

    if state["aborted"]:
        # User confirmed mid-burn cancel: surface as KeyboardInterrupt so
        # cmd_burn's per-disc handler prints the resume hint and the
        # top-level handler exits 130.
        raise KeyboardInterrupt
    if proc.returncode != 0:
        if device_busy:
            raise DeviceBusyError(device)
        raise subprocess.CalledProcessError(proc.returncode, cmd)
