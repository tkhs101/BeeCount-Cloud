## fork: default category seeding

Upstream keeps the default categories ONLY in the Flutter App
(`seed_service.dart`), pushed up via sync on first launch. The server and the
web client have none of them -- a fresh ledger comes back with
`/read/workspace/categories` = `[]`. Without the App there is no seeder at all,
so a new ledger has not a single category (not even "餐饮").

This fork seeds 44 default categories when `POST /write/ledgers` runs
(`src/services/default_categories.py`, an editable constant table), including
「税与保险」 with 消费税 / 所得税 / 社会保险 under it.

- Rule: **once per user** -- only when the user has no categories at all.
  Keying idempotency on `(name, kind)` cannot respect a user rename (rename
  餐饮 to 吃饭 and the next ledger re-adds 餐饮).
- Switch: `SEED_DEFAULT_CATEGORIES=false`.
- Parent/child is carried by `parent_name` + level 1/2: the mutator only writes
  `parentName`, and `projection.upsert_category` resolves the parent by
  (user_id, name, kind, level=1) -- so the parent must be persisted first.

