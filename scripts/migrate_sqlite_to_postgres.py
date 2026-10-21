"""Copy a LitLoom SQLite snapshot into a new, empty PostgreSQL database.

This is a data-transfer tool, not the PostgreSQL application backend. It does
not modify the SQLite source and refuses to write into a non-empty target.
PostgreSQL full-text indexes are derived data and must be rebuilt by the target
application after its PostgreSQL backend is installed.

Install the optional driver with ``python -m pip install -e .[postgres]`` and
set ``LITLOOM_DATABASE_URL`` in the environment. The URL is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _sqlalchemy():
    try:
        import sqlalchemy as sa
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError(
            "PostgreSQL 迁移依赖未安装。请运行：python -m pip install -e .[postgres]"
        ) from exc
    return sa


def source_tables(engine) -> tuple[list[str], list[str], list[str]]:
    """Return ordinary application tables and excluded SQLite virtual tables."""
    sa = _sqlalchemy()
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        ordinary: list[str] = []
        virtual: list[str] = []
        ddl_by_name: dict[str, str] = {}
        for name, ddl in rows:
            ddl_by_name[str(name)] = str(ddl or "")
            if str(ddl or "").lstrip().casefold().startswith("create virtual table"):
                virtual.append(str(name))
            else:
                ordinary.append(str(name))
        # FTS5 creates implementation tables that look like ordinary tables in
        # sqlite_master. They are derived indexes and must not be copied as data.
        try:
            shadows = [
                str(row[1])
                for row in connection.exec_driver_sql("PRAGMA table_list")
                if len(row) > 2 and row[2] == "shadow"
            ]
        except Exception:
            shadows = []
    # FTS5 labels external-content tables as shadow tables when their names
    # follow the virtual table's ``*_content`` convention. LitLoom's two
    # application-owned content tables share that convention and contain
    # durable search text, so preserve them when their DDL has domain FKs.
    durable_search_content = {
        name
        for name, parent in (
            ("paper_search_content", "papers"),
            ("paper_page_search_content", "document_pages"),
        )
        if name in ddl_by_name
        and f"references {parent}" in ddl_by_name[name].casefold()
    }
    shadow_set = set(shadows) - durable_search_content
    ordinary = [name for name in ordinary if name not in shadow_set]
    return ordinary, virtual, sorted(shadow_set)


def inspect_source(sqlite_path: Path) -> dict[str, Any]:
    """Inspect source table counts without connecting to or changing a target."""
    sa = _sqlalchemy()
    if not sqlite_path.is_file():
        raise ValueError(f"SQLite 文件不存在：{sqlite_path}")
    source = sa.create_engine(sa.engine.URL.create("sqlite", database=str(sqlite_path)))
    try:
        tables, virtual, shadows = source_tables(source)
        inspector = sa.inspect(source)
        counts = {}
        with source.connect() as connection:
            for name in tables:
                quoted = inspector.dialect.identifier_preparer.quote(name)
                counts[name] = int(
                    connection.execute(
                        sa.text(f"SELECT COUNT(*) FROM {quoted}")
                    ).scalar_one()
                )
        return {
            "source": str(sqlite_path.resolve()),
            "table_count": len(tables),
            "row_count": sum(counts.values()),
            "tables": counts,
            "excluded_sqlite_virtual_tables": virtual,
            "excluded_sqlite_shadow_tables": shadows,
            "postgres_search_indexes_rebuild_required": True,
        }
    finally:
        source.dispose()


def reflect_metadata(source, names: list[str]):
    """Reflect application tables and restore SQLite's username collation rule."""
    sa = _sqlalchemy()
    metadata = sa.MetaData()
    metadata.reflect(bind=source, only=names)
    users = metadata.tables.get("users")
    if users is not None and "username" in users.c:
        # SQLite's UNIQUE COLLATE NOCASE is not represented in reflected
        # SQLAlchemy metadata. Keep its case-insensitive account identity rule.
        sa.Index(
            "users_username_casefold_uq",
            sa.func.lower(users.c.username),
            unique=True,
        )
    return metadata


def validate_postgres_schema(sqlite_path: Path) -> dict[str, Any]:
    """Compile reflected table and index DDL with SQLAlchemy's PostgreSQL dialect."""
    sa = _sqlalchemy()
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    if not sqlite_path.is_file():
        raise ValueError(f"SQLite 文件不存在：{sqlite_path}")
    source = sa.create_engine(sa.engine.URL.create("sqlite", database=str(sqlite_path)))
    try:
        names, virtual, shadows = source_tables(source)
        metadata = reflect_metadata(source, names)
        dialect = postgresql.dialect()
        statements = []
        for table in metadata.sorted_tables:
            statements.append(str(CreateTable(table).compile(dialect=dialect)))
            statements.extend(
                str(CreateIndex(index).compile(dialect=dialect))
                for index in sorted(table.indexes, key=lambda item: item.name or "")
            )
        return {
            "status": "postgresql_ddl_compiles",
            "table_count": len(metadata.tables),
            "index_count": sum(
                len(table.indexes) for table in metadata.tables.values()
            ),
            "excluded_sqlite_virtual_tables": virtual,
            "excluded_sqlite_shadow_tables": shadows,
            "ddl_statement_count": len(statements),
            "username_case_insensitive_unique_index": "users" in metadata.tables
            and "username" in metadata.tables["users"].c,
            "note": "DDL compilation checks syntax only; a live PostgreSQL integration run is still required.",
        }
    finally:
        source.dispose()


def migrate(
    sqlite_path: Path, database_url: str, batch_size: int = 500
) -> dict[str, Any]:
    """Reflect a SQLite snapshot and copy its application tables atomically."""
    sa = _sqlalchemy()
    if not sqlite_path.is_file():
        raise ValueError(f"SQLite 文件不存在：{sqlite_path}")
    if not database_url or not database_url.startswith(
        ("postgresql://", "postgresql+psycopg://")
    ):
        raise ValueError(
            "LITLOOM_DATABASE_URL 必须是 PostgreSQL URL（postgresql+psycopg://...）"
        )
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://") :]
    if batch_size < 1 or batch_size > 10_000:
        raise ValueError("batch_size 必须在 1 到 10000 之间")

    source = sa.create_engine(sa.engine.URL.create("sqlite", database=str(sqlite_path)))
    target = sa.create_engine(database_url, pool_pre_ping=True)
    try:
        names, excluded_virtual, excluded_shadows = source_tables(source)
        if not names:
            raise ValueError("SQLite 文件中没有可迁移的 LitLoom 数据表")

        existing = sa.inspect(target).get_table_names()
        if existing:
            raise ValueError(
                "目标 PostgreSQL 必须为空；为避免覆盖已有数据，本工具拒绝继续。"
            )

        metadata = reflect_metadata(source, names)
        tables = metadata.sorted_tables
        migrated: dict[str, int] = {}

        # PostgreSQL DDL and inserts share one transaction. Any schema or data
        # error rolls the target back, leaving the original SQLite file intact.
        with target.begin() as destination:
            metadata.create_all(destination, checkfirst=False)
            with source.connect().execution_options(stream_results=True) as origin:
                for table in tables:
                    result = origin.execute(sa.select(table))
                    copied = 0
                    while rows := result.fetchmany(batch_size):
                        payload = [dict(row._mapping) for row in rows]
                        destination.execute(table.insert(), payload)
                        copied += len(payload)
                    migrated[table.name] = copied

            # SQLite INTEGER PRIMARY KEY values are copied as-is. Advance any
            # PostgreSQL-generated sequences so the next insert cannot collide.
            preparer = destination.dialect.identifier_preparer
            for table in tables:
                for column in table.primary_key.columns:
                    if not column.autoincrement:
                        continue
                    table_name = (
                        f"{table.schema}.{table.name}" if table.schema else table.name
                    )
                    sequence = destination.execute(
                        sa.text(
                            "SELECT pg_get_serial_sequence(:table_name,:column_name)"
                        ),
                        {"table_name": table_name, "column_name": column.name},
                    ).scalar_one_or_none()
                    if not sequence:
                        continue
                    table_sql = preparer.format_table(table)
                    column_sql = preparer.quote(column.name)
                    maximum = destination.execute(
                        sa.text(f"SELECT MAX({column_sql}) FROM {table_sql}")
                    ).scalar_one_or_none()
                    destination.execute(
                        sa.text(
                            "SELECT setval(CAST(:sequence_name AS regclass), :value, :called)"
                        ),
                        {
                            "sequence_name": sequence,
                            "value": int(maximum) if maximum is not None else 1,
                            "called": maximum is not None,
                        },
                    )

            # Multilingual substring queries and local PDF text need indexes
            # that match the application's existing LIKE/ILIKE predicates.
            destination.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            for table_name, columns in {
                "paper_search_content": (
                    "title", "authors", "abstract", "topic", "tags", "notes", "reading_card"
                ),
                "paper_page_search_content": ("text",),
                "document_pages": ("text",),
            }.items():
                if table_name not in metadata.tables:
                    continue
                for column in columns:
                    if column not in metadata.tables[table_name].c:
                        continue
                    index_name = f"{table_name}_{column}_trgm_idx"
                    destination.exec_driver_sql(
                        f'CREATE INDEX "{index_name}" ON "{table_name}" USING gin ("{column}" gin_trgm_ops)'
                    )

            # Verify copied row counts before committing the transaction.
            preparer = destination.dialect.identifier_preparer
            for table in tables:
                table_sql = preparer.format_table(table)
                actual = int(
                    destination.execute(
                        sa.text(f"SELECT COUNT(*) FROM {table_sql}")
                    ).scalar_one()
                )
                if actual != migrated[table.name]:
                    raise RuntimeError(
                        f"表 {table.name} 行数校验失败：源 {migrated[table.name]}，目标 {actual}"
                    )

        return {
            "status": "copied",
            "table_count": len(migrated),
            "row_count": sum(migrated.values()),
            "tables": migrated,
            "excluded_sqlite_virtual_tables": excluded_virtual,
            "excluded_sqlite_shadow_tables": excluded_shadows,
            "postgres_search_indexes_rebuild_required": False,
            "postgres_multilingual_trigram_indexes": True,
            "sqlite_source_modified": False,
        }
    finally:
        target.dispose()
        source.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_file", type=Path, help="一致性 SQLite 快照或数据库文件")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--inspect",
        action="store_true",
        help="只统计源数据，不连接 PostgreSQL、不写数据",
    )
    modes.add_argument(
        "--validate-schema",
        action="store_true",
        help="用 PostgreSQL 方言预编译源库 schema，不连接 PostgreSQL、不写数据",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    arguments = parser.parse_args(argv)

    try:
        if arguments.inspect:
            result = inspect_source(arguments.sqlite_file)
        elif arguments.validate_schema:
            result = validate_postgres_schema(arguments.sqlite_file)
        else:
            database_url = os.environ.get("LITLOOM_DATABASE_URL", "").strip()
            if not database_url:
                raise ValueError(
                    "请先在当前进程环境变量中设置 LITLOOM_DATABASE_URL；为避免泄露凭据，不建议把密码写入命令行。"
                )
            result = migrate(arguments.sqlite_file, database_url, arguments.batch_size)
    except Exception as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
