---
name: rename-live-variant
description: Rename a card that is already live as a variation on an eBay multi-variation listing and push the corrected name live. Use whenever the user asks to fix / rename the name of a card that is active on eBay (e.g. "fix the Classic Collection names", "rename this variant and push it live"). Method, as taught by the user - remove the card from eBay live, copy its picture, create a new roster entry on the listing template with the correct name, push it live. Same card in the DB, new roster row. Dry-run first, one card at a time, never touches other variations.
---

# Rename a live eBay variant

eBay does not reliably rename a variation in place (`importer/rename_variation.py`
`apply` / `bulk-apply` resends the whole Variations block and is a known-fragile
path, see CLAUDE.md "Known bug"). So a rename is done as:

1. **Remove** the card from the live eBay listing.
2. **Copy** its picture.
3. **Create a new roster entry** on the listing template with the correct name.
4. **Push** it live.

The card in the DB stays the same card (same `card_variants` row), so stock,
market prices, sales history and photos stay attached. Only the roster row
(`listing_card_assignments`) and the live eBay text change.

The card is off the listing between steps 1 and 4 (minutes). Do those back to
back. Never leave a card removed without telling the user.

## How the live name is made

`_stage_promotion()` in `importer/ebay_pushprices.py` uses the roster row's
`custom_name` verbatim when set, otherwise `render_variation_name(variant_id,
template_id)` (card name + number, formatted by the template's `name_format`).
The new roster row carries the correct name in `custom_name`, so the push
produces exactly that text no matter what `card_master.name` says.

## Step 0: Look at the row and check it can be pushed back

```sql
select lca.id row_id, lca.status, lca.template_id, lca.variant_id, lca.group_id,
       lca.priority_rank, lca.custom_name, lca.manual_price, lca.low_stock_qty,
       lca.quantity_limit, lca.card_photo_id, lca.eps_picture_url,
       cm.name card_name, cm.card_number, pl.external_id live_text, pl.listing_id,
       (select coalesce(sum(quantity-quantity_sold),0) from inventory i
         where i.variant_id=lca.variant_id and i.is_graded=false) stock
from listing_card_assignments lca
join card_variants cv on cv.id=lca.variant_id
join card_master cm on cm.id=cv.card_id
left join platform_listings pl on pl.id=lca.platform_listing_id
where lca.id = '<row_id>';
```

Stop and tell the user if:
- `status` is not `active`. A `queued` row has nothing live to remove; skip to
  Step 2.
- `stock` is 0. eBay silently drops a variation added at quantity 0
  (`push_single_card_live` refuses it), so you could not push it back. Do not
  remove it.
- There is no picture to copy (`card_photo_id` and `eps_picture_url` both null
  and no `card_photos` row for the variant). The picture set on eBay is keyed
  on the variation text, so removing the variation deletes its picture on eBay;
  without a stored photo the card would come back with no picture.

Write the old `row_id`, live text, group, rank, price pin, limits and photo
down before changing anything. You need them to rebuild the row.

## Step 1: Remove from eBay live

```bash
python3 main.py --ebay-remove-card --row-id <ROW_ID> --dry-run
python3 main.py --ebay-remove-card --row-id <ROW_ID>
```

`remove_single_card_live` deletes only that `<Variation>`, marks its
`platform_listings` row `delisted` with `sync_enabled=false`, and puts the
roster row back to `queued` (`platform_listing_id` cleared). Nothing else on the
listing is touched.

## Step 2: Create the new roster entry with the correct name

Run from the database, in one transaction. The new row copies everything from
the old one except `id`, the status/link, and the name; it points at the SAME
`card_photo_id` (that is the "copy the picture", the photo row already holds the
image on R2) and gets the corrected `custom_name`. Then the old queued row is
deleted so the template does not list the card twice. Nothing references
`listing_card_assignments.id`, and `(platform_listing_id, variant_id)` is the
only unique key, so this is safe.

```sql
begin;
insert into listing_card_assignments
  (template_id, variant_id, group_id, priority_rank, status, manual_price,
   low_stock_qty, quantity_limit, custom_name, eps_picture_url, card_photo_id)
select template_id, variant_id, group_id, priority_rank, 'queued', manual_price,
       low_stock_qty, quantity_limit, '<CORRECT NAME>', eps_picture_url, card_photo_id
from listing_card_assignments where id = '<old_row_id>'
returning id;                       -- note this as <new_row_id>

delete from listing_card_assignments where id = '<old_row_id>' and status = 'queued';
commit;
```

If the old row has no `card_photo_id` but a photo exists, set it on the new row:
`select id from card_photos where variant_id = '<variant_id>'` and use that id.

Keep `priority_rank` so manual-sort listings (for example the ME55 30th
Celebration template) keep the card in the same slot. Do not change the group
or price pin unless the user asks.

If the card's own name in `card_master` is also wrong, that is a separate
change the user decides on (it affects other listings and the inventory pages).
Do not rename it unprompted.

## Step 3: Push it live

```bash
python3 main.py --ebay-push-card --row-id <NEW_ROW_ID> --dry-run
python3 main.py --ebay-push-card --row-id <NEW_ROW_ID>
```

`push_single_card_live` adds one new `<Variation>` with the `custom_name`, the
resolved price and quantity (available minus low-stock, capped by the quantity
limit), re-attaches the photo from `card_photo_id`, and inserts it at the
position `display_sort` gives it. It writes the DB only after eBay accepts the
revise: the `platform_listings` row for this variant on this listing is
reactivated in place with the new `external_id`, the new roster row becomes
`active`, and `ebay_listing_map` gets a row for the new name. The old-name
`ebay_listing_map` row stays, so old orders keep matching. Never delete it.

## Step 4: Verify

```bash
python3 -m importer.rename_variation list <LISTING_ID>
```

New text live, old text gone, price and quantity as expected. And in the DB:

```sql
select lca.status, lca.custom_name, pl.external_id, pl.list_price, pl.quantity_listed, pl.status
from listing_card_assignments lca join platform_listings pl on pl.id = lca.platform_listing_id
where lca.id = '<new_row_id>';

select variation_name from ebay_listing_map
where item_id='<LISTING_ID>' and variant_id='<variant_id>';   -- old and new name
```

Check the picture is attached (open the listing, or look for the new text in
`VariationSpecificPictureSet`).

## Things the first live run (Classic Metagross, 2026-11-12) showed

- Run commands with `python`, not `python3` (`python3` is not installed on this
  machine; CLAUDE.md says otherwise). Use `PYTHONIOENCODING=utf-8`.
- **The price can change.** The push prices the card fresh from
  `resolve_listing_prices()` (group tier on the current market price), not from
  the old live price. Metagross went from $19.99 live to $11.99 (market $4.55,
  group tier). Before Step 1 compare `pl.list_price` with the resolved price
  (`select price_source, resolved_price from resolve_listing_prices('ebay',
  '<LISTING_ID>') where row_id = '<row_id>'`) and tell the user if they differ,
  so they can pin a price (`manual_price`) first if they want to keep it.
- **The old name lingers on eBay's side.** After the removal, `list` still shows
  the old text in `VariationSpecificsSet` and in the picture sets (removal
  deliberately does not prune the menu value). No variation row uses it. It is
  harmless for buyers but it is the kind of leftover behind the `rename_variation`
  count-mismatch bug, so mention it in the report.

## Report

Per card: old text -> new text, price, quantity. List first any card that
errored or is still removed. Say plainly if anything is not live yet.

## Rules

- Show the user the list of old -> new names before the first live call in a
  session, and get a yes. After that, one listing at a time: finish and verify
  before the next.
- If a push fails after a successful remove, fix the cause and push again
  straight away (usual causes: zero available quantity, 250-variation cap, no
  photo). Never leave the card off the listing.
- Do not use `rename_variation.py apply` / `bulk-apply` for this unless the user
  asks.
- Name conventions the user has set: hyphen before GX/EX becomes a space
  (`Solgaleo GX`); plain ASCII for eBay search (é -> e, ♀ -> ` F`, ♂ -> ` M`);
  Classic Collection names are `(Classic) Name Actual Number`, where the number
  is the original set's number and total from TCGplayer (`customAttributes.number`),
  never guessed.
- The roster row's price may come from a price group that has sync off. The
  push uses the resolved price; mention it if it differs from the old live price.
