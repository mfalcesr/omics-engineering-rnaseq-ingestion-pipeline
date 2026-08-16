"""Environment-driven configuration, no hardcoded credentials.

The connection is composed from ``DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD``
(twelve-factor style), or taken wholesale from ``DATABASE_URL`` if that is set. Values are
read from the process environment, with a ``.env`` file loaded as a convenience for local runs.
"""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass
from urllib.parse import quote

from dotenv import load_dotenv

# Load .env if present. Real environment variables always win over the file, so this is safe
# in orchestrated/production contexts where secrets come from a secret manager, not a file.
load_dotenv(override=False)


@dataclass(frozen=True)
class Settings:
    """Resolved connection settings, assembled once from the environment."""

    host: str
    port: int
    name: str
    user: str
    password: str
    actor: str

    @property
    def database_url(self) -> str:
        """A libpq/psycopg-compatible connection string."""
        override = os.getenv("DATABASE_URL")
        if override:
            return override
        # quote() so passwords with URL-special characters do not corrupt the DSN.
        return (
            f"postgresql://{quote(self.user)}:{quote(self.password)}"
            f"@{self.host}:{self.port}/{self.name}"
        )


def load_settings() -> Settings:
    """Build :class:`Settings` from the environment, matching the docker-compose defaults."""
    return Settings(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        name=os.getenv("DB_NAME", "rnaseq_db"),
        user=os.getenv("DB_USER", "rnaseq_user"),
        password=os.getenv("DB_PASSWORD", "rnaseq_password"),
        # Recorded in sample.ingested_by for the audit trail.
        actor=os.getenv("INGEST_ACTOR") or _current_actor(),
    )


def _current_actor() -> str:
    try:
        return f"{getpass.getuser()}@ingest-cli"
    except Exception:  # pragma: no cover - getuser can fail in odd environments
        return "ingest-cli"
