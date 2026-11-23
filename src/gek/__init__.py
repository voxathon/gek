"""gek: an asynchronous swarm harness with git as the shared medium."""

__version__ = "0.1.0"


def main() -> None:
    from .cli import main as _main

    _main()
