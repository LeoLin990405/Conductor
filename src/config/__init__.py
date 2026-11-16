# The server imports this package as ``src.config`` (cwd = repo root). A CLI or
# TUI process run from any cwd reaches ``config.runtime`` (R9b L08) as
# top-level ``config``: there ``src`` is not importable (``core`` needs
# ``src.config.models``), and before L08 those processes never loaded ``.env``,
# so neither happens under that name.
if __name__ == "src.config":
    from dotenv import load_dotenv

    from .core import load_yaml_config

    # Load environment variables
    load_dotenv()

    __all__ = [
        # Utilities
        "load_yaml_config",
    ]
