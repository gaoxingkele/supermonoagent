"""Local accounts. Each one has a password and a token. The relay only accepts those tokens."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{1,63}$")


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), 100_000).hex()


class Accounts:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.users: list[dict] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.users = []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.users = []
            return
        self.users = list(data.get("users") or []) if isinstance(data, dict) else []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"users": self.users}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def tokens(self) -> list[str]:
        return [str(user.get("token") or "") for user in self.users if user.get("token")]

    def register(self, email: str, password: str) -> str:
        label = (email or "").strip().lower()
        if not _EMAIL.match(label):
            raise ValueError("要填写邮箱")
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        if self._user(label) is not None:
            raise ValueError("邮箱已注册")
        salt = secrets.token_hex(8)
        token = secrets.token_urlsafe(18)
        self.users.append({"email": label, "salt": salt, "password": _hash(password, salt), "token": token})
        self.save()
        return token

    def login(self, email: str, password: str) -> str:
        user = self._user(email)
        if user is None or user.get("password") != _hash(password or "", str(user.get("salt") or "")):
            raise ValueError("邮箱或密码不对")
        return str(user["token"])

    def rotate(self, email: str, password: str) -> str:
        user = self._user(email)
        if user is None or user.get("password") != _hash(password or "", str(user.get("salt") or "")):
            raise ValueError("邮箱或密码不对")
        user["token"] = secrets.token_urlsafe(18)
        self.save()
        return str(user["token"])

    def _user(self, email: str) -> dict | None:
        label = (email or "").strip().lower()
        for user in self.users:
            if user.get("email") == label or user.get("name") == label:
                return user
        return None
