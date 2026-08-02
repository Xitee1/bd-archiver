import sys
import time

from bd_archive.ui.logger import Logger, log


def styled_input(prompt: str) -> str:
    """input() with the standard yellow prompt styling, colors gated on
    the same TTY check as the logger so piped output stays clean."""
    y, r = Logger._c("yellow"), Logger._c("reset")
    return input(f"{y}{prompt}{r}")


def prompt_disc(label: str, device: str):
    log.banner(f"{label}  —  Device: {device}")
    resp = styled_input("Press Enter when ready (q = cancel): ")
    if resp.strip().lower() == "q":
        log.warn("Cancelled by user")
        sys.exit(0)
    time.sleep(3)


def prompt_yn(question: str, default_yes: bool = True) -> bool:
    """Ask a yes/no question. Only clear answers are accepted: y/yes,
    n/no, or Enter for the default — anything else re-asks. Substrings
    like "no" must never fall through to the default-yes branch when the
    next step is hours of disc burning."""
    hint = "Y/n" if default_yes else "y/N"
    while True:
        resp = styled_input(f"{question} ({hint}): ").strip().lower()
        if resp == "":
            return default_yes
        if resp in ("y", "yes"):
            return True
        if resp in ("n", "no"):
            return False
        log.warn("Please answer 'y' or 'n'.")
