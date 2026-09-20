"""Settings loading: environment variables, a Worker `env` binding object, or a `.env` file.

Pure stdlib -- must run on CPython and inside a Cloudflare Python Worker (Pyodide).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

_ENV_KEYS = (
    "GOOGLE_MAPS_API_KEY",
    "ONEMAP_EMAIL",
    "ONEMAP_PASSWORD",
    "ROUTING_ENGINE",
    "LOCAL_DB",
)


@dataclass(frozen=True, slots=True)
class Settings:
    google_maps_api_key: str | None = None
    onemap_email: str | None = None
    onemap_password: str | None = None
    routing_engine: str = "google"
    local_db: str = "data/local.sqlite"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings from a mapping (defaults to ``os.environ``)."""
        source = env if env is not None else os.environ
        return cls(
            google_maps_api_key=source.get("GOOGLE_MAPS_API_KEY") or None,
            onemap_email=source.get("ONEMAP_EMAIL") or None,
            onemap_password=source.get("ONEMAP_PASSWORD") or None,
            routing_engine=(source.get("ROUTING_ENGINE") or "google").strip().lower(),
            local_db=source.get("LOCAL_DB") or "data/local.sqlite",
        )

    @classmethod
    def from_object(cls, obj: object) -> Settings:
        """Read settings from an object's attributes (e.g. a Cloudflare Worker ``env`` binding)."""
        return cls(
            google_maps_api_key=getattr(obj, "GOOGLE_MAPS_API_KEY", None) or None,
            onemap_email=getattr(obj, "ONEMAP_EMAIL", None) or None,
            onemap_password=getattr(obj, "ONEMAP_PASSWORD", None) or None,
            routing_engine=(getattr(obj, "ROUTING_ENGINE", None) or "google").strip().lower(),
            local_db=getattr(obj, "LOCAL_DB", None) or "data/local.sqlite",
        )


def load_dotenv(path: str = ".env") -> dict[str, str]:
    """Tiny ``.env`` parser: ``KEY=VALUE`` lines.

    Ignores blank lines and full-line comments (``#...``), strips a trailing
    ``  # comment`` from unquoted values, and strips matching surrounding
    quotes. Does not mutate ``os.environ`` -- caller decides what to do with
    the result. Missing files yield an empty dict (no python-dotenv dependency).
    """
    result: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return result

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # Strip an inline `  # comment` from unquoted values.
            hash_index = value.find("#")
            if hash_index != -1:
                value = value[:hash_index].rstrip()

        result[key] = value

    return result
