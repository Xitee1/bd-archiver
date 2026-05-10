import sys


class Logger:
    """Colored console output."""

    COLORS = {
        "red": "\033[0;31m", "green": "\033[0;32m",
        "yellow": "\033[1;33m", "blue": "\033[0;34m",
        "cyan": "\033[0;36m", "bold": "\033[1m", "reset": "\033[0m",
    }

    @classmethod
    def _c(cls, name: str) -> str:
        return cls.COLORS[name] if sys.stdout.isatty() else ""

    @classmethod
    def info(cls, msg: str):
        print(f"{cls._c('blue')}[INFO]{cls._c('reset')}  {msg}")

    @classmethod
    def ok(cls, msg: str):
        print(f"{cls._c('green')}[  OK]{cls._c('reset')}  {msg}")

    @classmethod
    def warn(cls, msg: str):
        print(f"{cls._c('yellow')}[WARN]{cls._c('reset')}  {msg}")

    @classmethod
    def error(cls, msg: str):
        print(f"{cls._c('red')}[ ERR]{cls._c('reset')}  {msg}", file=sys.stderr)

    @classmethod
    def step(cls, msg: str):
        print(f"\n{cls._c('cyan')}{cls._c('bold')}── {msg} ──{cls._c('reset')}")

    @classmethod
    def banner(cls, msg: str):
        b, c, r = cls._c("bold"), cls._c("cyan"), cls._c("reset")
        print(f"\n{b}{c}╔{'═' * 62}╗{r}")
        print(f"{b}{c}║  {msg:<60s}║{r}")
        print(f"{b}{c}╚{'═' * 62}╝{r}\n")


log = Logger
