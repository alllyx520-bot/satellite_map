from pathlib import Path


def load_project_env(base_dir=None):
    """Load project .env once for every Django entrypoint."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False

    root = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    return bool(load_dotenv(root / ".env", override=False))
