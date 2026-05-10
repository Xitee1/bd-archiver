from bd_archive.shell.runner import run


def eject(device: str):
    run(["eject", device], capture=True, check=False)
