import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent
DATA = PACKAGE / "data"


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    database: Path = DATA / "footypreds.sqlite3"
    cache_ttl: int = 900
    history_ttl: int = 6 * 3600

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env")
        path = Path(os.getenv("DATABASE_PATH", "data/footypreds.sqlite3"))
        return cls(
            api_key=os.getenv("RAPIDAPI_KEY", ""),
            # Relative paths are resolved inside the single project folder.
            database=path if path.is_absolute() else PACKAGE / path,
            cache_ttl=max(60, int(os.getenv("CACHE_TTL_SECONDS", "900"))),
            history_ttl=max(300, int(os.getenv("HISTORY_CACHE_TTL_SECONDS", str(6 * 3600)))),
        )
