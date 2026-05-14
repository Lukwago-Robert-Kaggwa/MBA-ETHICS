"""Small Alembic helpers for databases that were previously synced manually."""

from alembic import op
import sqlalchemy as sa


def _inspector():
    return sa.inspect(op.get_bind())


def table_exists(table_name, schema=None):
    return _inspector().has_table(table_name, schema=schema)


def column_exists(table_name, column_name, schema=None):
    if not table_exists(table_name, schema=schema):
        return False
    return any(column["name"] == column_name for column in _inspector().get_columns(table_name, schema=schema))


def add_column_if_missing(table_name, column, schema=None):
    if column_exists(table_name, column.name, schema=schema):
        return False
    op.add_column(table_name, column, schema=schema)
    return True


def index_exists(table_name, index_name, schema=None):
    if not table_exists(table_name, schema=schema):
        return False
    return any(index["name"] == index_name for index in _inspector().get_indexes(table_name, schema=schema))


def unique_constraint_exists(table_name, constraint_name, schema=None):
    if not table_exists(table_name, schema=schema):
        return False
    return any(
        constraint["name"] == constraint_name
        for constraint in _inspector().get_unique_constraints(table_name, schema=schema)
    )


def named_index_or_constraint_exists(table_name, name, schema=None):
    return index_exists(table_name, name, schema=schema) or unique_constraint_exists(table_name, name, schema=schema)


def create_index_if_missing(index_name, table_name, columns, unique=False, schema=None):
    if named_index_or_constraint_exists(table_name, index_name, schema=schema):
        return False
    op.create_index(index_name, table_name, columns, unique=unique, schema=schema)
    return True


def create_unique_constraint_if_missing(constraint_name, table_name, columns, schema=None):
    if named_index_or_constraint_exists(table_name, constraint_name, schema=schema):
        return False
    op.create_unique_constraint(constraint_name, table_name, columns, schema=schema)
    return True


def create_table_if_missing(table_name, *columns, **kwargs):
    schema = kwargs.get("schema")
    if table_exists(table_name, schema=schema):
        return False
    op.create_table(table_name, *columns, **kwargs)
    return True


def foreign_key_exists(table_name, local_columns, referred_table=None, constraint_name=None, schema=None):
    if not table_exists(table_name, schema=schema):
        return False
    local_columns = list(local_columns)
    for foreign_key in _inspector().get_foreign_keys(table_name, schema=schema):
        if constraint_name and foreign_key["name"] == constraint_name:
            return True
        if list(foreign_key.get("constrained_columns") or []) != local_columns:
            continue
        if referred_table and foreign_key.get("referred_table") != referred_table:
            continue
        return True
    return False


def create_foreign_key_if_missing(constraint_name, source_table, referent_table, local_cols, remote_cols):
    if foreign_key_exists(source_table, local_cols, referent_table, constraint_name):
        return False
    op.create_foreign_key(constraint_name, source_table, referent_table, local_cols, remote_cols)
    return True
