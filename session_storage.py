"""Separate each browser session's saves and replays on a hosted server."""
from pathlib import Path
import tempfile
import uuid


def storage_paths(session_id: str):
    session_id = str(uuid.UUID(session_id))
    root = Path(tempfile.gettempdir()) / "silicon_wolf_village" / session_id
    return str(root / "saves"), str(root / "replays")
