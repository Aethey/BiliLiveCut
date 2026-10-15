"""Docker 管理员凭据、一次性初始化及可撤销会话的私有持久化存储。"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

ITERATIONS = 600_000
SETUP_TTL = 1800
COOKIE_NAME = "blc_session"


class AuthError(ValueError):
    """可向用户显示、且不含输入凭据的认证失败。"""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.status = status


def password_hash(password: str) -> str:
    """使用独立随机盐和 PBKDF2-SHA256 保存密码，不存储明文。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS)
    return f"{salt}:{digest.hex()}"


def password_matches(password: str, verifier: str) -> bool:
    """使用固定成本和常量时间比较验证凭据。"""
    if len(password) > 1024:
        return False
    salt, digest = verifier.split(":")
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS)
    return secrets.compare_digest(candidate.hex(), digest)


def token_hash(token: str) -> str:
    """只保存高熵会话及初始化码的 SHA-256 摘要。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_password(password: str) -> None:
    """新密码支持中文和密码短语；长度为 12 至 128 字符且不允许纯空白。"""
    if (
        not 12 <= len(password) <= 128
        or not password.strip()
        or any(unicodedata.category(c) in {"Cc", "Cs"} for c in password)
    ):
        raise AuthError("密码须为 12–128 个字符，可使用中文或密码短语，不能为纯空白或含控制字符。")


def _private_path(path: Path, *, directory: bool = False) -> None:
    metadata = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode):
        raise RuntimeError(f"认证路径必须为真实的{'目录' if directory else '普通文件'}，不能使用符号链接：{path}")
    if os.name == "posix":
        forbidden = 0o022 if directory else 0o077
        if metadata.st_uid != os.getuid() or metadata.st_mode & forbidden:
            raise RuntimeError(f"认证路径必须属于容器用户，目录不可被其他用户写入、文件权限须为 600：{path}")


class AuthStore:
    """通过短 SQLite 事务串行更新凭据和会话，改密、退出与重置立即生效。"""

    def __init__(self, root: Path, *, idle_seconds: int = 1800, session_seconds: int = 43200) -> None:
        self.path = root / "config" / "web-auth.sqlite3"
        self.idle_seconds = idle_seconds
        self.session_seconds = session_seconds

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """仅打开已存在的私有认证库，运行中丢失文件时失败关闭。"""
        _private_path(self.path.parent, directory=True)
        _private_path(self.path)
        db = sqlite3.connect(f"{self.path.as_uri()}?mode=rw", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def create(self) -> None:
        """初始化私有文件与固定 Schema；不覆盖现有或损坏的认证数据。"""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private_path(self.path.parent, directory=True)
        created = False
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
            created = True
        with self.connect() as db:
            if not created:
                if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise RuntimeError("认证库为空、损坏或格式未知，请恢复备份，不会自动覆盖。")
                self._row(db)
                return
            db.execute(
                "CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1), "
                "mode TEXT NOT NULL, verifier TEXT NOT NULL, setup_hash TEXT NOT NULL, setup_expires REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL, "
                "role TEXT NOT NULL, csrf TEXT NOT NULL, created REAL NOT NULL, seen REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS failures "
                "(ip TEXT PRIMARY KEY, count INTEGER NOT NULL, started REAL NOT NULL)"
            )
            db.execute("PRAGMA user_version = 1")

    @staticmethod
    def _row(db: sqlite3.Connection) -> sqlite3.Row:
        row = db.execute("SELECT * FROM identity WHERE id=1").fetchone()
        if row is None or row["mode"] not in {"setup", "password", "environment"}:
            raise RuntimeError("认证库未初始化或已损坏，请恢复备份。")
        if row["mode"] == "setup":
            if not re.fullmatch(r"[0-9a-f]{64}", row["setup_hash"]) or row["setup_expires"] <= 0:
                raise RuntimeError("初始化凭据损坏，请恢复备份。")
        elif not re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{64}", row["verifier"]):
            raise RuntimeError("密码摘要损坏，请恢复备份。")
        return row

    def boot(self, configured_password: str, legacy_password: str | None = None) -> str | None:
        """入口调用：保留已设密码，迁移旧密码，轮换未使用的初始化码并撤销旧会话。"""
        self.create()
        with self.connect() as db:
            row = db.execute("SELECT * FROM identity WHERE id=1").fetchone()
            db.execute("DELETE FROM sessions")
            # 重启不清除失败冷却，避免通过反复重启绕过登录限流。
            if configured_password.strip():
                self._identity(db, "environment", password_hash(configured_password))
                return None
            if row is not None and row["mode"] == "password":
                return None
            if row is None and legacy_password is not None:
                self._identity(db, "password", password_hash(legacy_password))
                return None
            return self._setup(db)

    @staticmethod
    def _identity(db: sqlite3.Connection, mode: str, verifier: str) -> None:
        db.execute("INSERT OR REPLACE INTO identity VALUES (1, ?, ?, '', 0)", (mode, verifier))

    @staticmethod
    def _setup(db: sqlite3.Connection) -> str:
        code = secrets.token_urlsafe(24)
        db.execute(
            "INSERT OR REPLACE INTO identity VALUES (1, 'setup', '', ?, ?)",
            (token_hash(code), time.time() + SETUP_TTL),
        )
        db.execute("DELETE FROM sessions")
        return code

    def state(self) -> dict[str, bool | int]:
        """返回公开初始化状态和会话时限，不暴露凭据摘要。"""
        with self.connect() as db:
            row = self._row(db)
            return {
                "enabled": True,
                "setup_required": row["mode"] == "setup",
                "managed_by_environment": row["mode"] == "environment",
                "idle_seconds": self.idle_seconds,
                "session_seconds": self.session_seconds,
            }

    @staticmethod
    def _rate(db: sqlite3.Connection, ip: str) -> None:
        now = time.time()
        db.execute("DELETE FROM failures WHERE started < ?", (now - 300,))
        row = db.execute("SELECT count FROM failures WHERE ip=?", (ip,)).fetchone()
        count = db.execute("SELECT count(*) FROM failures").fetchone()[0]
        if (row is not None and row["count"] >= 5) or (row is None and count >= 1000):
            raise AuthError("登录尝试过于频繁，请在 5 分钟后重试。", 429)

    @staticmethod
    def _failure(db: sqlite3.Connection, ip: str) -> None:
        db.execute(
            "INSERT INTO failures VALUES (?, 1, ?) ON CONFLICT(ip) DO UPDATE SET count=count+1",
            (ip, time.time()),
        )

    def initialize(self, code: str, password: str, ip: str) -> None:
        """一次性核销有效初始化码，原子保存首个管理员密码。"""
        validate_password(password)
        valid = False
        with self.connect() as db:
            self._rate(db, ip)
            row = self._row(db)
            if row is None or row["mode"] != "setup":
                raise AuthError("初始化已完成，请直接登录。", 409)
            valid = row["setup_expires"] > time.time() and secrets.compare_digest(row["setup_hash"], token_hash(code))
            if valid:
                self._identity(db, "password", password_hash(password))
                db.execute("DELETE FROM sessions")
                db.execute("DELETE FROM failures WHERE ip=?", (ip,))
            else:
                self._failure(db, ip)
        if not valid:
            raise AuthError("初始化码无效或已过期，请从当前容器日志获取；过期后重启容器生成新码。", 401)

    def authenticate(
        self,
        username: str,
        password: str,
        reviewers: dict[str, str],
        ip: str,
        *,
        create_session: bool = False,
    ) -> dict[str, str] | None:
        """验证管理员或审核员；失败次数保留在事务中，成功后解除该 IP 冷却计数。"""
        role = None
        with self.connect() as db:
            self._rate(db, ip)
            row = self._row(db)
            if row is None or row["mode"] == "setup":
                raise AuthError("请先完成管理员初始化。", 401)
            if username == "admin" and password_matches(password, row["verifier"]):
                role = "admin"
            elif username != "admin" and username in reviewers:
                if secrets.compare_digest(password.encode("utf-8"), reviewers[username].encode("utf-8")):
                    role = "reviewer"
            if role:
                db.execute("DELETE FROM failures WHERE ip=?", (ip,))
                token = self._issue_session(db, username, role) if create_session else ""
                # 在验证密码的同一事务内捕获版本；不保留明文或可用于验密的摘要。
                credential_version = token_hash(row["verifier"]) if role == "admin" else ""
            else:
                self._failure(db, ip)
        return {"role": role, "token": token, "credential_version": credential_version} if role else None

    def _admin_authorized(self, db: sqlite3.Connection, session: str, credential_version: str) -> bool:
        if session:
            self._prune(db, time.time())
            row = db.execute("SELECT role FROM sessions WHERE token=?", (token_hash(session),)).fetchone()
            return row is not None and row["role"] == "admin"
        identity = self._row(db)
        return bool(
            credential_version
            and identity["mode"] != "setup"
            and secrets.compare_digest(token_hash(identity["verifier"]), credential_version)
        )

    def admin_authorized(self, *, session: str = "", credential_version: str = "") -> bool:
        """检查已认证任务的会话或密码版本，不延长会话或保存原始凭据。"""
        with self.connect() as db:
            return self._admin_authorized(db, session, credential_version)

    def run_admin_action(self, action: Callable[[], None], *, session: str = "", credential_version: str = "") -> None:
        """在撤销事务锁内核验并执行短写入，避免改密完成后仍提交旧任务。"""
        with self.connect() as db:
            if not self._admin_authorized(db, session, credential_version):
                raise AuthError("管理员登录已失效，请重新登录并申请二维码。", 401)
            action()

    def _issue_session(self, db: sqlite3.Connection, username: str, role: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._prune(db, now)
        db.execute(
            "DELETE FROM sessions WHERE token IN "
            "(SELECT token FROM sessions WHERE username=? ORDER BY created DESC LIMIT -1 OFFSET 9)",
            (username,),
        )
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
            (token_hash(token), username, role, secrets.token_urlsafe(32), now, now),
        )
        return token

    def _prune(self, db: sqlite3.Connection, now: float) -> None:
        db.execute(
            "DELETE FROM sessions WHERE created <= ? OR seen <= ?",
            (now - self.session_seconds, now - self.idle_seconds),
        )

    def session(self, token: str, *, touch: bool = True) -> dict[str, str] | None:
        """检查绝对有效期及空闲期限；状态探测不会延长会话寿命。"""
        if not token or len(token) > 128:
            return None
        now = time.time()
        with self.connect() as db:
            self._prune(db, now)
            row = db.execute("SELECT * FROM sessions WHERE token=?", (token_hash(token),)).fetchone()
            if row is None:
                return None
            if touch:
                db.execute("UPDATE sessions SET seen=? WHERE token=?", (now, token_hash(token)))
            return {"username": row["username"], "role": row["role"], "csrf_token": row["csrf"]}

    def logout(self, token: str, *, everywhere: bool = False) -> None:
        """撤销当前会话，或撤销该账号的所有浏览器会话。"""
        with self.connect() as db:
            if everywhere:
                db.execute(
                    "DELETE FROM sessions WHERE username=(SELECT username FROM sessions WHERE token=?)",
                    (token_hash(token),),
                )
            else:
                db.execute("DELETE FROM sessions WHERE token=?", (token_hash(token),))

    def change_password(self, token: str, current: str, password: str, ip: str) -> None:
        """管理员验证旧密码后修改密码，所有旧会话立即失效。"""
        validate_password(password)
        valid = False
        with self.connect() as db:
            self._prune(db, time.time())
            session = db.execute("SELECT role FROM sessions WHERE token=?", (token_hash(token),)).fetchone()
            if session is None or session["role"] != "admin":
                raise AuthError("请以管理员身份重新登录。", 401)
            self._rate(db, ip)
            row = self._row(db)
            if row["mode"] == "environment":
                raise AuthError("密码由 ADMIN_PASSWORD 管理，请修改部署环境后重建容器。", 409)
            valid = password_matches(current, row["verifier"])
            if valid:
                self._identity(db, "password", password_hash(password))
                db.execute("DELETE FROM sessions")
                db.execute("DELETE FROM failures WHERE ip=?", (ip,))
            else:
                self._failure(db, ip)
        if not valid:
            raise AuthError("当前密码不正确。", 403)

    def reset(self) -> str:
        """容器操作者主动重置密码，立即撤销会话并生成新的单次初始化码。"""
        with self.connect() as db:
            row = self._row(db)
            if row is None or row["mode"] == "environment":
                raise AuthError("密码由 ADMIN_PASSWORD 管理，请修改部署环境后重建容器。")
            return self._setup(db)


def main() -> int:
    """提供 Docker 操作者使用的密码重置命令，不删除业务数据。"""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["reset"])
    parser.parse_args()
    root = Path(os.environ.get("BLC_APP_ROOT", "/data")).resolve()
    try:
        code = AuthStore(root).reset()
    except (AuthError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"密码重置失败：{exc}", file=sys.stderr)
        return 1
    print(f"管理员初始化码（30 分钟内有效，仅可使用一次）：{code}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
