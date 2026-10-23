from typing import TYPE_CHECKING

import pytest

from mappd.db.lint import check, check_file, migration_files
from mappd.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

HEADER = "from alembic import op\nimport sqlalchemy as sa\n\n"


def write(tmp_path: Path, body: str, name: str = "m.py") -> Path:
    path = tmp_path / name
    path.write_text(HEADER + body)
    return path


@pytest.mark.parametrize(
    "call",
    [
        'op.drop_table("t")',
        'op.drop_column("t", "c")',
        'op.drop_index("i")',
        'op.drop_constraint("c", "t")',
        'op.rename_table("a", "b")',
        'op.alter_column("t", "a", new_column_name="b")',
        'op.execute("ALTER TABLE t DROP COLUMN c")',
        'op.execute("drop table t")',
        'op.execute("ALTER TABLE a RENAME TO b")',
        'op.execute("DROP EXTENSION vector")',
        'op.execute("DROP TRIGGER t_audit ON t")',
        'op.execute(sa.text("DROP TABLE t"))',
        'op.execute(text("ALTER TABLE a RENAME TO b"))',
    ],
)
def test_destructive_operations_in_upgrade_are_violations(tmp_path: Path, call: str) -> None:
    path = write(tmp_path, f"def upgrade() -> None:\n    {call}\n")
    [violation] = check_file(path)
    assert violation.path == path
    assert violation.line == 5
    assert "contract = True" in violation.message


@pytest.mark.parametrize(
    "call",
    [
        'op.add_column("t", sa.Column("c", sa.Text()))',
        'op.create_table("t")',
        'op.alter_column("t", "c", nullable=True)',
        'op.execute("ALTER TABLE t ALTER COLUMN c DROP NOT NULL")',
        'op.execute("ALTER TABLE t ALTER COLUMN c DROP DEFAULT")',
        'op.execute("CREATE INDEX i ON t (c)")',
    ],
)
def test_expand_operations_pass(tmp_path: Path, call: str) -> None:
    assert check_file(write(tmp_path, f"def upgrade() -> None:\n    {call}\n")) == []


def test_downgrade_may_drop(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        'def upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    op.drop_table("t")\n',
    )
    assert check_file(path) == []


def test_contract_migration_may_drop(tmp_path: Path) -> None:
    path = write(
        tmp_path, 'contract = True\n\n\ndef upgrade() -> None:\n    op.drop_column("t", "c")\n'
    )
    assert check_file(path) == []


def test_contract_false_does_not_exempt(tmp_path: Path) -> None:
    path = write(
        tmp_path, 'contract = False\n\n\ndef upgrade() -> None:\n    op.drop_column("t", "c")\n'
    )
    assert len(check_file(path)) == 1


def test_violation_prints_as_path_line_message(tmp_path: Path) -> None:
    [violation] = check_file(write(tmp_path, 'def upgrade() -> None:\n    op.drop_table("t")\n'))
    assert str(violation).startswith(f"{tmp_path / 'm.py'}:5: ")


def test_check_collects_from_every_file(tmp_path: Path) -> None:
    a = write(tmp_path, 'def upgrade() -> None:\n    op.drop_table("a")\n', "a.py")
    b = write(tmp_path, 'def upgrade() -> None:\n    op.drop_table("b")\n', "b.py")
    assert [v.path for v in check([a, b])] == [a, b]


def test_core_migrations_pass(tmp_path: Path) -> None:
    files = migration_files(Settings(processes_dir=tmp_path))
    assert files, "the core ships at least one migration"
    assert all(f.name != "__init__.py" for f in files)
    assert check(files) == []
