"""PostgreSQL DB-API compatibility for LitLoom's existing SQL call sites.

SQLite remains the isolated test/import backend. Production PostgreSQL uses
psycopg directly so transactions, constraints, and concurrency are handled by
the database server rather than a process-local file lock.
"""

from __future__ import annotations

import re
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

class Row(dict):
    """Mapping row that preserves SQLite's integer-position access pattern."""

    def __getitem__(self, key):
        if isinstance(key, int):
            key = tuple(self.keys())[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    names = tuple(column.name for column in cursor.description or ())

    def build(values):
        return Row(zip(names, values))

    return build


def _replace_qmarks(statement: str) -> str:
    """Convert qmark binds without touching quoted SQL text or comments."""
    result: list[str] = []
    index = 0
    quote = ""
    while index < len(statement):
        char = statement[index]
        next_char = statement[index + 1] if index + 1 < len(statement) else ""
        if quote:
            result.append(char)
            if char == quote:
                if next_char == quote:
                    result.append(next_char)
                    index += 1
                else:
                    quote = ""
        elif char in {"'", '"', "`"}:
            quote = char
            result.append(char)
        elif char == "-" and next_char == "-":
            end = statement.find("\n", index)
            if end < 0:
                result.append(statement[index:])
                break
            result.append(statement[index : end + 1])
            index = end
        elif char == "?":
            result.append("%s")
        else:
            result.append(char)
        index += 1
    return "".join(result)


def _translate(statement: str) -> str:
    query = statement.strip()
    ignore_insert = bool(re.match(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\b", query))
    replace_insert = bool(re.match(r"(?is)^INSERT\s+OR\s+REPLACE\s+INTO\b", query))
    query = re.sub(r"(?is)^INSERT\s+OR\s+(?:IGNORE|REPLACE)\s+INTO\b", "INSERT INTO", query)
    # SQLite counts predicates in SUM; PostgreSQL requires an integer CASE.
    query = re.sub(
        r"(?is)SUM\(\s*([\w.]+)\s*=\s*('(?:''|[^'])*'|\d+)\s*\)",
        r"SUM(CASE WHEN \1=\2 THEN 1 ELSE 0 END)",
        query,
    )
    query = re.sub(r"(?is)MAX\(([^(),]+),([^(),]+)\)", r"GREATEST(\1,\2)", query)
    query = re.sub(r"(?is)\binstr\(\s*([^,]+),\s*([^()]+)\)", r"strpos(\1,\2)", query)
    query = re.sub(r"(?is)\bjson_valid\(([^()]+)\)", "TRUE", query)
    query = re.sub(
        r"(?is)json_extract\(\s*([^,]+),\s*'\$\.([A-Za-z0-9_]+)'\s*\)",
        r"(\1::jsonb ->> '\2')",
        query,
    )
    if ignore_insert and not re.search(r"(?is)\bON\s+CONFLICT\b", query):
        query += " ON CONFLICT DO NOTHING"
    elif replace_insert and not re.search(r"(?is)\bON\s+CONFLICT\b", query):
        match = re.match(r"(?is)^INSERT\s+INTO\s+[^ (]+\s*\(([^)]+)\)", query)
        columns = [item.strip().strip('"') for item in match.group(1).split(",")] if match else []
        updates = [column for column in columns if column.casefold() not in {"id", "rowid"}]
        if updates:
            assignments = ",".join(f"{column}=EXCLUDED.{column}" for column in updates)
            query += f" ON CONFLICT DO UPDATE SET {assignments}"
        else:
            query += " ON CONFLICT DO NOTHING"
    query = re.sub(r"(?is)\s+COLLATE\s+NOCASE\b", "", query)
    query = re.sub(r"(?is)\bLIKE\b", "ILIKE", query)
    return _replace_qmarks(query)


class Cursor:
    def __init__(self, connection, cursor, statement: str):
        self._connection = connection
        self._cursor = cursor
        self._statement = statement

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        match = re.match(r"(?is)^INSERT\s+INTO\s+([\w\"]+)", self._statement.strip())
        if not match:
            return None
        table = match.group(1).strip('"')
        with self._connection._raw.cursor() as cursor:
            cursor.execute(
                "SELECT currval(pg_get_serial_sequence(%s, 'id'))",
                (table,),
            )
            result = cursor.fetchone()
            return result[0] if result else None

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size: int | None = None):
        return self._cursor.fetchmany(size) if size else self._cursor.fetchmany()

    def __iter__(self):
        return iter(self._cursor)


class Connection:
    def __init__(self, database_url: str):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL 驱动未安装，请运行 python -m pip install -e '.[postgres]'"
            ) from exc
        self._psycopg = psycopg
        database_url = database_url.replace(
            "postgresql+psycopg://", "postgresql://", 1
        )
        self._raw = psycopg.connect(database_url, row_factory=_row_factory)
        self._total_changes = 0

    @property
    def total_changes(self) -> int:
        return self._total_changes

    def execute(self, statement: str, parameters: Any = ()) -> Cursor:
        translated = _translate(statement)
        cursor = self._raw.cursor()
        try:
            cursor.execute(translated, parameters)
        except self._psycopg.IntegrityError as exc:
            cursor.close()
            raise sqlite3.IntegrityError(str(exc)) from exc
        except self._psycopg.OperationalError as exc:
            cursor.close()
            raise sqlite3.OperationalError(str(exc)) from exc
        except Exception:
            cursor.close()
            raise
        if cursor.rowcount > 0 and not translated.lstrip().upper().startswith("SELECT"):
            self._total_changes += cursor.rowcount
        return Cursor(self, cursor, statement)

    def executemany(self, statement: str, parameter_rows) -> Cursor:
        translated = _translate(statement)
        cursor = self._raw.cursor()
        try:
            cursor.executemany(translated, parameter_rows)
        except self._psycopg.IntegrityError as exc:
            cursor.close()
            raise sqlite3.IntegrityError(str(exc)) from exc
        except self._psycopg.OperationalError as exc:
            cursor.close()
            raise sqlite3.OperationalError(str(exc)) from exc
        except Exception:
            cursor.close()
            raise
        if cursor.rowcount > 0:
            self._total_changes += cursor.rowcount
        return Cursor(self, cursor, statement)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False

    @property
    def in_transaction(self) -> bool:
        return (
            self._raw.info.transaction_status
            != self._psycopg.pq.TransactionStatus.IDLE
        )


def connect(database_url: str) -> Connection:
    return Connection(database_url)


def _postgres_client(name: str) -> Path:
    configured = os.environ.get("LITLOOM_POSTGRES_BIN_DIR", "").strip()
    if configured:
        path = Path(configured) / (name + (".exe" if os.name == "nt" else ""))
        if path.is_file():
            return path
    from shutil import which

    found = which(name)
    if found:
        return Path(found)
    raise RuntimeError(
        f"找不到 PostgreSQL {name}；请设置 LITLOOM_POSTGRES_BIN_DIR 或将客户端加入 PATH"
    )


def ensure_local_server(database_url: str) -> None:
    """Start LitLoom's user-owned PostgreSQL cluster when it is not running."""
    database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = urlsplit(database_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return
    initial_error = None
    try:
        import psycopg

        with psycopg.connect(database_url, connect_timeout=2):
            return
    except Exception as exc:
        initial_error = exc
        data_dir = Path(os.environ.get("LITLOOM_POSTGRES_DATA_DIR", "")).expanduser()
        if not data_dir.is_dir():
            raise RuntimeError(
                "PostgreSQL 无法连接，且本机数据目录未配置；检查 PostgreSQL 服务与 LITLOOM_DATABASE_URL"
            ) from initial_error
    log_path = data_dir.parent / "postgresql.log"
    process = subprocess.Popen(
        [
            str(_postgres_client("pg_ctl")),
            "start",
            "-D",
            str(data_dir),
            "-l",
            str(log_path),
            "-o",
            f"-h {parsed.hostname} -p {parsed.port or 5432}",
            "-w",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=(
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
            if os.name == "nt"
            else 0
        ),
    )
    deadline = time.monotonic() + 30
    last_error = initial_error
    while time.monotonic() < deadline:
        if process.poll() not in (None, 0):
            break
        try:
            with psycopg.connect(database_url, connect_timeout=1):
                return
        except Exception as exc:
            last_error = exc
            time.sleep(0.3)
    raise RuntimeError(
        "LitLoom 本机 PostgreSQL 无法启动；请检查数据目录日志与端口占用"
    ) from last_error


def _client_environment(database_url: str) -> tuple[list[str], dict[str, str]]:
    from sqlalchemy.engine import make_url

    url = make_url(database_url.replace("postgresql+psycopg://", "postgresql://", 1))
    args = []
    if url.host:
        args.extend(["-h", url.host])
    if url.port:
        args.extend(["-p", str(url.port)])
    if url.username:
        args.extend(["-U", url.username])
    if url.database:
        args.extend(["-d", url.database])
    environment = os.environ.copy()
    if url.password:
        environment["PGPASSWORD"] = url.password
    for key in ("sslmode", "sslrootcert", "sslcert", "sslkey", "gssencmode"):
        value = url.query.get(key)
        if value:
            environment["PG" + key.upper()] = value
    return args, environment


def dump_database(database_url: str, destination: Path) -> None:
    args, environment = _client_environment(database_url)
    command = [
        str(_postgres_client("pg_dump")),
        *args,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(destination),
    ]
    try:
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True, timeout=600
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("PostgreSQL 备份超过 10 分钟，已停止") from exc
    if result.returncode:
        raise RuntimeError("PostgreSQL 备份失败：" + result.stderr.strip()[-500:])


def validate_dump(dump_path: Path) -> None:
    result = subprocess.run(
        [str(_postgres_client("pg_restore")), "--list", str(dump_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise ValueError("PostgreSQL 备份格式无效")


def restore_dump(database_url: str, dump_path: Path) -> None:
    validate_dump(dump_path)
    args, environment = _client_environment(database_url)
    command = [
        str(_postgres_client("pg_restore")),
        *args,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--single-transaction",
        str(dump_path),
    ]
    try:
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True, timeout=1800
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("PostgreSQL 恢复超过 30 分钟，已停止") from exc
    if result.returncode:
        raise RuntimeError("PostgreSQL 恢复失败：" + result.stderr.strip()[-700:])


def database_label(database_url: str) -> str:
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    endpoint = f"{url.host or 'local'}:{url.port}" if url.port else (url.host or "local")
    return f"PostgreSQL · {endpoint}/{url.database or ''}"


def table_exists(db: Connection, table_name: str) -> bool:
    return bool(
        db.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=?",
            (table_name,),
        ).fetchone()
    )


def integrity_check(db: Connection) -> str:
    db.execute("SELECT 1").fetchone()
    return "ok"


def migrate_source(sqlite_path, database_url: str) -> dict:
    from scripts.migrate_sqlite_to_postgres import migrate

    return migrate(sqlite_path, database_url)


def apply_migrations(database_url: str) -> None:
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL 迁移依赖未安装，请运行 python -m pip install -e '.[postgres]'"
        ) from exc
    migration_dir = Path(__file__).with_name("migrations")
    configuration = Config()
    configuration.set_main_option("script_location", str(migration_dir))
    configuration.set_main_option(
        "sqlalchemy.url", database_url.replace("%", "%%")
    )
    command.upgrade(configuration, "head")
