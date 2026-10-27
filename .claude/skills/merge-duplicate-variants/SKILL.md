---
name: merge-duplicate-variants
description: Merge duplicate card_variants rows in the Card-Board-MasterMind Supabase schema (two variant rows that should be one real card). Use whenever asked to merge/dedupe/consolidate card variants, or when an import/review surfaces more than one card_variants row for a card that should only have one (e.g. "typical promos only have one variant"). Encodes a hard-won procedure — a prior attempt at this broke because it wrote to a view as if it were a table and lost data it should have moved instead.
---

# Merging duplicate card_variants

This exists because a 2026-10-19 merge attempt skipped straight to writing SQL
from memory and got two things wrong: it issued `UPDATE` against a *view*
(`ebay_inventory_status`), which would have failed the whole transaction on
commit, and it `DELETE`d a `market_prices` row that should have been
re-pointed instead, losing data. The fix cost a full night of rework. Follow
every step below in order — don't shortcut to writing the merge SQL from
memory of what tables "usually" need touching.

## Step 1 — Discover dependents programmatically

Never hand-list the tables that reference `card_variants` from memory. Query it:

```sql
-- FK-declared dependents
select tc.table_name, kcu.column_name
from information_schema.table_constraints tc
join information_schema.key_column_usage kcu on kcu.constraint_name = tc.constraint_name
join information_schema.constraint_column_usage ccu on ccu.constraint_name = tc.constraint_name
where tc.constraint_type = 'FOREIGN KEY' and ccu.table_name = 'card_variants';

-- every variant_id column, FK-declared or not (inventory and market_prices
-- are plain columns here, not FKs, and won't show up in the query above)
select table_name, column_name from information_schema.columns where column_name = 'variant_id';
```

**Then, for every table name that comes back, check `table_type` before writing to it:**

```sql
select table_name, table_type from information_schema.tables where table_name in (...);
```

A `variant_id` column does not mean it's a writable table. `ebay_inventory_status`
specifically is a VIEW (joins `ebay_listing_map`, `card_variants`, `inventory`) —
it updates itself automatically once `ebay_listing_map.variant_id` is repointed.
Writing to it directly errors the whole transaction.

## Step 2 — Inspect before writing, per pair

For each duplicate pair, pull every dependent row for both variant IDs and compare:

- Row counts per table for each variant.
- `inventory.acquired_at` — **this, not the variant row's own creation date, is
  what tells you which variant is "the real one to keep."** `push_staging_row_to_inventory`
  does a null-safe exact-axis match against *existing* variants, so new stock
  can land on a card_variants row that's months old (e.g. it already serves a
  different listing template). "Newest variant row" and "the one that just
  received this batch's inventory" are not the same thing — use `acquired_at`.
- Whether either variant already has live eBay data (`platform_listings`,
  `ebay_listing_map`, `sales`) — that one usually needs to be the survivor, or
  at minimum its live-listing rows must be repointed, not dropped.

## Step 3 — Check unique constraints before re-pointing, in both directions

Known constraints in this schema:
- `market_prices`: unique on `(variant_id, condition)`
- `platform_listings`: unique on `(variant_id, platform, listing_id, external_id)`
- `listing_card_assignments`: effectively one row per `(template_id, variant_id)`

For each dependent table: if the keep-side variant already has an equivalent
row, **delete** the old-side row (re-pointing would violate the constraint).
If the keep-side has no equivalent row, **re-point** the old-side row (deleting
would silently lose real data — this is exactly the market_prices mistake that
caused the rework).

## Step 4 — Order the writes correctly

Re-point (or delete, per Step 3) every dependent table first, including `sales`
(it has a no-action FK — delete the variant before clearing `sales` and the
transaction fails). Delete the old `card_variants` row **last**.

## Step 5 — One transaction, then verify

Wrap the whole thing in `BEGIN; ... COMMIT;`. End with a verification query:

```sql
-- should return zero rows
select id from card_variants where id in (<all old variant ids>);
-- should return zero rows (no card should have more than one variant, for
-- cards where that's the expectation)
select card_id, count(*) from card_variants group by card_id having count(*) > 1;
```

## Step 6 — Get explicit approval before running the destructive transaction

Name the exact pairs and the exact tables affected before executing. Don't run
a multi-pair merge transaction speculatively.
