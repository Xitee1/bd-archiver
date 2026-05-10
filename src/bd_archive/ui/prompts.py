import sys
import time

from bd_archive.ui.logger import log


def prompt_disc(label: str, device: str):
    log.banner(f"{label}  —  Device: {device}")
    resp = input("\033[1;33mPress Enter when ready (q = cancel): \033[0m")
    if resp.strip().lower() == "q":
        log.warn("Cancelled by user")
        sys.exit(0)
    time.sleep(3)


def prompt_yn(question: str, default_yes: bool = True) -> bool:
    hint = "Y/n" if default_yes else "y/N"
    resp = input(f"\033[1;33m{question} ({hint}): \033[0m").strip().lower()
    return resp != "n" if default_yes else resp == "y"
