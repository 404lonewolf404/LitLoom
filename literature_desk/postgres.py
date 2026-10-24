"""SQLAlchemy-backed PostgreSQL access for LitLoom.

SQLAlchemy owns the production engine, connection pool, execution, and
transactions. A small DB-API-shaped adapter keeps the existing domain SQL
call sites stable while they are migrated deliberately. SQLite remains
outside this module's runtime path.
"""

from __future__ import annotations

import re
import os
import sqlite3
import subprocess
import time
from pathlib import Path
import threading
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import IntegrityError, OperationalError


_POOL_LOCK = threading.RLock()
_ENGINES: dict[str, Engine] = {}

class Row(dict):
    """Mapping row that preserves SQLite's integer-position access pattern."""

    def __getitem__(self, key):
        if isinstance(key, int):
            key = tuple(self.keys())[key]
        return super().__getitem__(key)


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
    def __init__(self, connection, result, statement: str):
        self._connection = connection
        self._result = result
        self._statement = statement

    @property
    def rowcount(self):
        return self._result.rowcount

    @property
    def lastrowid(self):
        match = re.match(r"(?is)^INSERT\s+INTO\s+([\w\"]+)", self._statement.strip())
        if not match:
            return None
        table = match.group(1).strip('"')
        result = self._connection._connection.exec_driver_sql(
            "SELECT currval(pg_get_serial_sequence(%s, 'id'))",
            (table,),
        )
        value = result.scalar_one_or_none()
        result.close()
        return value

    @staticmethod
    def _row(row):
        if row is None:
            return None
        mapping = getattr(row, "_mapping", None)
        return Row(mapping.items() if mapping is not None else enumerate(row))

    def fetchone(self):
        return self._row(self._result.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self._result.fetchall()]

    def fetchmany(self, size: int | None = None):
        rows = self._result.fetchmany(size) if size else self._result.fetchmany()
        return [self._row(row) for row in rows]

    def __iter__(self):
        return (self._row(row) for row in self._result)


class Connection:
    """Small compatibility adapter over a SQLAlchemy Core connection."""

    def __init__(self, engine: Engine):
        try:
            self._connection = engine.connect()
        except Exception as exc:
            raise RuntimeError(
                "无法连接 PostgreSQL；请检查数据库服务和连接池配置"
            ) from exc
        self._total_changes = 0
        self._closed = False

    @property
    def total_changes(self) -> int:
        return self._total_changes

    def execute(self, statement: str, parameters: Any = ()) -> Cursor:
        translated = _translate(statement)
        try:
            result = self._connection.exec_driver_sql(translated, parameters)
        except IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc.orig)) from exc
        except OperationalError as exc:
            raise sqlite3.OperationalError(str(exc.orig)) from exc
        if result.rowcount > 0 and not translated.lstrip().upper().startswith("SELECT"):
            self._total_changes += result.rowcount
        return Cursor(self, result, statement)

    def executemany(self, statement: str, parameter_rows) -> Cursor:
        translated = _translate(statement)
        try:
            result = self._connection.exec_driver_sql(translated, parameter_rows)
        except IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc.orig)) from exc
        except OperationalError as exc:
            raise sqlite3.OperationalError(str(exc.orig)) from exc
        if result.rowcount > 0:
            self._total_changes += result.rowcount
        return Cursor(self, result, statement)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                if self._connection.in_transaction():
                    self._connection.rollback()
            except Exception:
                pass
            finally:
                self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction()


def _engine_for(database_url: str) -> Engine:
    """Return the process-wide SQLAlchemy engine for a PostgreSQL URL."""
    url = _sqlalchemy_url(database_url)
    normalized = url.render_as_string(hide_password=False)
    with _POOL_LOCK:
        engine = _ENGINES.get(normalized)
        if engine is not None:
            return engine
        max_size = _pool_size("LITLOOM_DB_POOL_MAX", 12, minimum=1, maximum=64)
        timeout = _pool_timeout()
        engine = create_engine(
            url,
            pool_size=max_size,
            max_overflow=0,
            pool_timeout=timeout,
            pool_pre_ping=True,
            connect_args={"connect_timeout": max(1, min(int(timeout), 30))},
        )
        _ENGINES[normalized] = engine
        return engine


def _sqlalchemy_url(database_url: str) -> URL:
    """Use psycopg 3 explicitly; SQLAlchemy otherwise defaults to psycopg2."""
    url = make_url(database_url)
    if url.drivername == "postgresql":
        return url.set(drivername="postgresql+psycopg")
    return url


def _pool_size(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _pool_timeout() -> float:
    try:
        value = float(os.environ.get("LITLOOM_DB_POOL_TIMEOUT", "30"))
    except (TypeError, ValueError):
        value = 30.0
    return max(1.0, min(300.0, value))


def connect(database_url: str) -> Connection:
    if not database_url or not database_url.startswith(
        ("postgresql://", "postgresql+psycopg://")
    ):
        raise ValueError("PostgreSQL 连接地址无效")
    return Connection(_engine_for(database_url))


def close_pools() -> None:
    """Close all process pools during an orderly application shutdown."""
    with _POOL_LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()
    for engine in engines:
        engine.dispose()


def pool_snapshot() -> dict:
    """Expose pool sizing and health without exposing connection strings."""
    with _POOL_LOCK:
        engines = list(_ENGINES.values())
    return {
        "pool_count": len(engines),
        "pools": [
            {
                "name": "litloom-postgres",
                "size": pool.size(),
                "available": pool.checkedin(),
                "checked_out": pool.checkedout(),
                "waiting": None,
            }
            for pool in (engine.pool for engine in engines)
        ],
    }


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
    data_dir = Path(os.environ.get("LITLOOM_POSTGRES_DATA_DIR", "")).expanduser()
    if not data_dir.is_dir():
        raise RuntimeError(
            "PostgreSQL 无法连接，且本机数据目录未配置；检查 PostgreSQL 服务与 LITLOOM_DATABASE_URL"
        )

    def cluster_running() -> bool:
        """Check the configured cluster, rather than any server on its port."""
        try:
            result = subprocess.run(
                [str(_postgres_client("pg_ctl")), "status", "-D", str(data_dir)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    initial_error = None
    try:
        import psycopg

        with psycopg.connect(database_url, connect_timeout=2):
            if cluster_running():
                return
            # A different PostgreSQL data directory can be listening on the
            # configured port. Silently accepting it makes LitLoom appear to
            # lose settings and papers after a restart, and can leave AI
            # controls disabled when that server is recovering.
            raise RuntimeError(
                f"端口 {parsed.port or 5432} 已被另一套 PostgreSQL 数据目录占用；"
                f"请停止占用该端口的实例，或将其数据目录设为 {data_dir}"
            )
    except Exception as exc:
        initial_error = exc
        if isinstance(exc, RuntimeError) and "另一套 PostgreSQL 数据目录" in str(exc):
            raise
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
            "PostgreSQL 迁移依赖未安装，请运行 python -m pip install -e ."
        ) from exc
    migration_dir = Path(__file__).with_name("migrations")
    configuration = Config()
    configuration.set_main_option("script_location", str(migration_dir))
    configuration.set_main_option(
        "sqlalchemy.url",
        _sqlalchemy_url(database_url).render_as_string(hide_password=False).replace(
            "%", "%%"
        ),
    )
    command.upgrade(configuration, "head")
