"""Decision D1: a tenant id is an opaque string, stored as TEXT.

The Booking Service owner has not decided the format - it is probably a clinic
username. Until they do, this repo must never parse, normalise or reformat one.
These tests are the structural half of that promise; `tests/tenants/
test_resolver.py` is the behavioural half.
"""

import ast
import pathlib

import pytest
import sqlalchemy as sa

from app.db.base import Base
from app.db.enums import Channel
from app.db.repositories import ContactRepository
from app.tenants import TenantId

APP = pathlib.Path(__file__).resolve().parents[2] / "app"


def test_tenant_id_is_a_plain_str_alias():
    """One place names the type (app/tenants/resolver.py), and it says `str`.

    `TenantId is str` rather than `issubclass`: a NewType or a str subclass
    would still be a place where a format could be smuggled back in.
    """
    assert TenantId is str


def test_every_tenant_id_column_is_text():
    """Every table that knows a tenant stores it as TEXT.

    sa.Uuid would reject "clinic-alpha" at the driver, so a single missed column
    is a runtime failure on the first real message rather than a drift warning.
    """
    columns = [
        (table.name, column)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name == "tenant_id"
    ]

    assert columns, "no tenant_id column found: the metadata import is wrong"
    for table_name, column in columns:
        assert isinstance(column.type, sa.Text), f"{table_name}.tenant_id is {column.type!r}"


def test_no_app_code_annotates_a_tenant_as_a_uuid():
    """An AST sweep, because a stale annotation is invisible at runtime.

    A `tenant_id: uuid.UUID` left behind type-checks against nothing (this repo
    runs no type checker) and reads as documentation that is now a lie. The
    sweep covers annotated assignments and function parameters alike.
    """
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, annotation = node.target.id, node.annotation
            elif isinstance(node, ast.arg) and node.annotation is not None:
                name, annotation = node.arg, node.annotation
            else:
                continue
            if "tenant" not in name.lower():
                continue
            rendered = ast.unparse(annotation)
            if "uuid" in rendered.lower():
                offenders.append(f"{path.relative_to(APP.parent)}:{node.lineno} {name}: {rendered}")

    assert offenders == []


@pytest.mark.db
async def test_two_tenants_that_differ_only_in_case_are_different_tenants(db_session):
    """Q2: matching is exact. The database must agree with the resolver.

    A case-insensitive column type (CITEXT, or a lower() index) would merge two
    clinics that the Booking Service considers distinct, and one clinic's
    patients would appear in the other's inbox.
    """
    upper = await ContactRepository(db_session, "Clinic-Alpha").get_or_create_by_identity(
        Channel.WHATSAPP, "96170000001"
    )
    lower = await ContactRepository(db_session, "clinic-alpha").get_or_create_by_identity(
        Channel.WHATSAPP, "96170000001"
    )

    assert upper.id != lower.id
    assert upper.tenant_id == "Clinic-Alpha"
    assert lower.tenant_id == "clinic-alpha"
