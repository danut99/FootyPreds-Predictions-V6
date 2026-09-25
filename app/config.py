import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    database: Path = ROOT / "data/footypreds.sqlite3"
    cache_ttl: int = 900

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env")
        path = Path(os.getenv("DATABASE_PATH", "data/footypreds.sqlite3"))
        return cls(
            api_key=os.getenv("RAPIDAPI_KEY", ""),
            database=path if path.is_absolute() else ROOT / path,
            cache_ttl=max(60, int(os.getenv("CACHE_TTL_SECONDS", "900"))),
        )
