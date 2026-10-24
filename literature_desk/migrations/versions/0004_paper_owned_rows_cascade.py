"""Restore paper-owned cascade semantics on PostgreSQL foreign keys."""

from alembic import op
from sqlalchemy import text

revision = "0004_paper_owned_cascade"
down_revision = "0003_deepseek_search_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    constraints = connection.execute(
        text(
            """SELECT tc.table_schema,tc.table_name,tc.constraint_name,
                      ccu.table_schema AS referenced_schema,
                      string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position) AS columns,
                      string_agg(ccu.column_name, ',' ORDER BY kcu.ordinal_position)
                        AS referenced_columns,
                      rc.update_rule,rc.match_option,
                      tc.is_deferrable,tc.initially_deferred
               FROM information_schema.table_constraints tc
               JOIN information_schema.key_column_usage kcu
                 ON (tc.constraint_catalog,tc.constraint_schema,tc.constraint_name)=
                    (kcu.constraint_catalog,kcu.constraint_schema,kcu.constraint_name)
               JOIN information_schema.constraint_column_usage ccu
                 ON (tc.constraint_catalog,tc.constraint_schema,tc.constraint_name)=
                    (ccu.constraint_catalog,ccu.constraint_schema,ccu.constraint_name)
               JOIN information_schema.referential_constraints rc
                 ON (tc.constraint_catalog,tc.constraint_schema,tc.constraint_name)=
                    (rc.constraint_catalog,rc.constraint_schema,rc.constraint_name)
               WHERE tc.constraint_type='FOREIGN KEY'
                 AND tc.table_schema=current_schema()
                 AND ccu.table_schema=current_schema()
                 AND ccu.table_name='papers'
                 AND rc.delete_rule <> 'CASCADE'
               GROUP BY tc.table_schema,tc.table_name,tc.constraint_name,
                        ccu.table_schema,rc.update_rule,rc.match_option,
                        tc.is_deferrable,tc.initially_deferred
               ORDER BY tc.table_name,tc.constraint_name"""
        )
    ).mappings()
    preparer = connection.dialect.identifier_preparer
    for row in constraints:
        table = f"{preparer.quote_schema(row['table_schema'])}.{preparer.quote(row['table_name'])}"
        parent = f"{preparer.quote_schema(row['referenced_schema'])}.\"papers\""
        constraint = preparer.quote(row["constraint_name"])
        columns = ", ".join(
            preparer.quote(value) for value in row["columns"].split(",")
        )
        references = ", ".join(
            preparer.quote(value) for value in row["referenced_columns"].split(",")
        )
        update = "" if row["update_rule"] == "NO ACTION" else f" ON UPDATE {row['update_rule']}"
        match = "" if row["match_option"] == "NONE" else f" MATCH {row['match_option']}"
        deferred = (
            " DEFERRABLE INITIALLY DEFERRED"
            if row["is_deferrable"] == "YES" and row["initially_deferred"] == "YES"
            else " DEFERRABLE"
            if row["is_deferrable"] == "YES"
            else ""
        )
        connection.exec_driver_sql(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        connection.exec_driver_sql(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
            f"FOREIGN KEY ({columns}) REFERENCES {parent} ({references})"
            f"{match}{update} ON DELETE CASCADE{deferred}"
        )


def downgrade() -> None:
    # Keep cascade semantics during rollback; removing them could make normal
    # paper deletion fail again on a restored older application version.
    pass
