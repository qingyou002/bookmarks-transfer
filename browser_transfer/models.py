from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
RUNTIME = PROJECT / "runtime"
ITEMS = ("bookmarks", "history", "cookies", "passwords", "autofill")
LABELS = {
    "bookmarks": "收藏夹", "history": "历史记录", "cookies": "Cookie",
    "passwords": "密码", "autofill": "地址与联系方式",
}


@dataclass(frozen=True)
class Profile:
    browser: str
    user_data: Path
    directory: str
    executable: Path | None = None
    display_name: str = ""

    @property
    def path(self) -> Path:
        return self.user_data / self.directory

    @property
    def key(self) -> str:
        import hashlib
        raw = f"{self.browser}:{self.path.resolve()}".casefold()
        return hashlib.sha256(raw.encode()).hexdigest()

    def to_dict(self) -> dict:
        return {"browser": self.browser, "user_data": str(self.user_data),
                "directory": self.directory,
                "executable": str(self.executable) if self.executable else None,
                "display_name": self.display_name}

    @classmethod
    def from_dict(cls, data: dict) -> Profile:
        return cls(data["browser"], Path(data["user_data"]), data["directory"],
                   Path(data["executable"]) if data.get("executable") else None,
                   data.get("display_name", ""))


@dataclass
class Counts:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass
class Result:
    status: str = "pending"
    counts: Counts = field(default_factory=Counts)
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class MigrationError(Exception):
    """A safe user-facing error; never put browser data in its message."""


class Cancelled(MigrationError):
    pass
