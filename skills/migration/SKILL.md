---
name: migration
description: Change the database schema of the MAPP-D core or a process with an expand-only Alembic migration. Use when adding a table, column or index, or when a change seems to need a drop, rename or type change.
---

# Migration

Deploys and rollbacks rely on **expand-only** migrations: the older code must keep running
on the newer schema; a rollback is a deploy of an older SHA.

## Where

| Owner | Folder | Revision ids |
| --- | --- | --- |
| core | `src/mappd/db/versions/` | `mappd_000N` |
| a process | `processes/<name>/migrations/` | `<name>_000N` |

A process's first migration sets `branch_labels = ("<name>",)` and
`depends_on = "mappd_0001"`; later ones chain with `down_revision`. Copy the newest file in
the folder for the header.

## How

- `op.create_table` / `op.add_column` / `op.create_index` with explicit `sa.Column`s. Never
  import `db.py` metadata into a migration: a migration must replay as written.
- `op.execute` only for DDL Core cannot express (`CREATE EXTENSION`) and for data backfills
  written as Core statements on a lightweight `sa.table`.
- A new column on a populated table is nullable or has a server default.
- Update the matching `sa.Table` in `db.py` (or `src/mappd/db/tables.py` for the core) in
  the same commit; a test compares them with the migrated database.

## Drops, renames, type changes

Not in one step. Expand now (add the new column, write both), migrate the readers, and drop
the old one in a **later deploy** with a migration marked `contract = True`. The linter
refuses a drop or rename without it.

## Check

```bash
uv run mappd db lint        # expand-only rule
uv run mappd db migrate     # applies to the dev databases
uv run pytest tests/db      # table metadata matches the migrations
```

Tests that list revisions (`tests/db/test_migrate.py`, `tests/cli/test_db.py`,
`tests/test_doctor.py`) name the core's head; a new core migration updates them.

Back up and restore the app and DBOS databases together, never separately
(`mappd db backup` / `mappd db restore`).
