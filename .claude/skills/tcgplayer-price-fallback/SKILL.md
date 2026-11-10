---
name: tcgplayer-price-fallback
description: Fallback for refreshing a set's market prices when the pokemontcg.io API returns no market price for some cards. Use whenever the user asks to refresh a set (or a specific card) and the API refresh leaves cards without a price, or says a card has no price. Claude drives the user's browser on TCGplayer, reads the Near Mint market price for ONLY those cards, checks listing counts, writes each price to market_prices, and reports only the exceptions (still no price, fewer than 15 listings, or cards it cannot match with confidence). Never touches cards the API already priced.
---

# TCGplayer price fallback

The pokemontcg.io API returns a market price for most cards. This skill covers
the cards it did not. Claude reads those prices from TCGplayer through the
user's browser (Claude in Chrome), writes them to the database, and reports
only the exceptions at the end.

## When to use

Use when the user asks to refresh a set's market prices (or one card's) and
the API leaves some cards with no price, or the user says a card has no price.
Do not use it as a general price refresh and never to override a price the API
returned.

Run the normal API refresh first (`python main.py --refresh-market-prices --set "SET NAME"`,
or the Jobs page). This skill starts after it finishes.

## Which cards are in scope

Only variants whose Near Mint `market_prices` row is missing or was not updated
today. The API refresh stamps `updated_at` with today's date on every price it
returns, so anything not stamped today got no API price. Anything stamped today
is skipped, no exceptions.

```sql
SELECT v.id AS variant_id, m.card_number, m.name, m.rarity,
       v.foil_type, v.foil_pattern, v.texture, mp.market_price AS current_price
FROM card_variants v
JOIN card_master m ON m.id = v.card_id
JOIN card_sets s ON s.id = m.set_id
LEFT JOIN market_prices mp ON mp.variant_id = v.id AND mp.condition = 'Near Mint'
WHERE s.set_code = '<set_code>'          -- or s.name = '<set name>'
  AND (mp.id IS NULL OR mp.updated_at::date < CURRENT_DATE)
ORDER BY m.card_number_numeric NULLS LAST, m.card_number;
```

Pattern variants (Poké Ball, Master Ball, Team Rocket, etc. — any variant with
`foil_pattern` set) are never priced by the API, so they are always in scope.
They are read from the set page as their own printing, not through the plain
reverse holo card-page step. If the user named a single card, restrict the query to that card.

## Browser setup (before reading any prices)

Open the set on TCGplayer (slug looks like `me06-delta-reign`; some sets have a
separate slug, e.g. a Classic Collection) and set these filters first:

1. Condition: **Near Mint**
2. Product type / card stock: **Card**
3. Sort: **A to Z**

Confirm all three are applied before reading anything.

## Fast path: read the same data through TCGplayer's own endpoints

Clicking through the pages works, but the same numbers are available much faster
by calling TCGplayer's endpoints from a `tcgplayer.com` tab with
`mcp__claude-in-chrome__javascript_tool` (the tab supplies the session; no
keys are needed). Used on Delta Reign, Pitch Black and 30th Celebration with
clean matches. The four steps below still define *what* to read and flag.

- **Set list (steps 1 and 3):** `POST https://mp-search-api.tcgplayer.com/v1/search/request?q=&isList=true`,
  page with `from`/`size:50`, `filters.term = {setName:[<slug>], productTypeName:['Cards']}`,
  sort `product-name` asc. Each product gives `customAttributes.number`, `marketPrice`,
  `totalListings`, `foilOnly`, `productId`. Ignore products with an empty number
  (Code Cards, sealed). Slugs: `me06-delta-reign`, `me05-pitch-black`,
  `me-30th-celebration` (Classic Collection has its own slug).
- **Per-printing prices (step 2):** `GET https://mpapi.tcgplayer.com/v2/product/<productId>/pricepoints`
  returns `Normal` and `Foil`. `Normal` is the non-holo price. `Foil` is the plain
  reverse holo price when a `Normal` exists, and the holo price when the card
  is holo-only (`Normal` null, no reverse holo listings).
- **Reverse holo listing count (steps 2 and 3):** `POST https://mp-search-api.tcgplayer.com/v1/product/<productId>/listings`
  with `filters.term = {sellerStatus:'Live', channelId:0, language:['English'], condition:['Near Mint'], printing:['Reverse Holofoil']}`,
  `range.quantity.gte = 1`, `size:1`; the count is `results[0].totalResults`.
- Match to the database by the card number (digits before the `/`). Anything with
  no match, or two products for one number, goes on the exception list.
- For sets with one variant per card (e.g. 30th Celebration) `marketPrice` from
  the set list is enough; round to 2 decimals. Mew promos use `R/RGB`, `B/RGB`, `G/RGB`.
- **Never price** variants with no `foil_type` (untyped placeholders) or with a
  `texture` (e.g. cosmos); list them as unmatched / unsure instead.
- Return the data in chunks of about 30 rows; long tool output gets cut off. A
  `NNN:normal:foil:rl:tl` text blob can be pasted into SQL and parsed with
  `regexp_split_to_table`/`split_part`, then written with the upsert below.
- If an endpoint stops working or a number looks wrong, fall back to reading the
  pages with the Near Mint / Card / A to Z filters.

## The four steps

### 1. Read the set page
Go through every page of the set (about 20–30 cards per page). For each card in
scope, read its market price straight off the page. Skip cards that are not in
scope. Match each page entry to the database card by number (`NNN/TOTAL`) and
name; if the name and number do not agree, or two entries could be the same
card, do not guess — put it on the exception list as unmatched.

### 2. Open plain reverse holo cards
**This step is only for plain reverse holos** — variants with
`foil_type = 'reverse_holo'` and **no** `foil_pattern`. A reverse holo with any
other name or pattern (Poké Ball, Master Ball, Team Rocket, …) is NOT handled
here; see "Pattern variants" below.

TCGplayer lumps the normal and plain reverse holo printings under one listing,
so the set page shows a single price. For any in-scope card that has a plain
reverse holo variant:
- open that card's page, where both prices are on screen;
- read the **reverse holo** market price from there;
- still on the card page, apply the **reverse holo** filter and read the listing
  count from that filtered view. That count is used for the flag check in step 3.

The plain reverse holo is always read from the individual card page.

**Pattern variants** (Poké Ball, Master Ball, Team Rocket, and any other
`foil_pattern`) are separate products on TCGplayer, each with its own listing,
price and listing count. Treat each as its own printing: find that specific
pattern product on the set page and read its price and count there, exactly like
any other non-reverse-holo printing. Do not use the card-page reverse holo
filter for them.

### 3. Check listing counts
The listing count is shown on the filtered set page next to each card, so there
is no need to open a card page for it. For every in-scope card and every
printing other than the plain reverse holo (holo, non-holo, and pattern
variants), read that count from the set page. For plain reverse holos use the
count from the reverse holo filter on the card page, not the set page.

If a count is **fewer than 15**, add the card to the flag list (the market price
may be unreliable). Still write the price; the flag is for the user to review.

### 4. Write and report
After each card's price is read, write it to the database (below). When the run
finishes, output **only the exceptions**. No mid-run alerts, no list of prices
found normally.

## Writing the price

Use the same upsert as `upsert_market_price()` in `db/connection.py`. It keeps
the previous price and date so the pricing page can show "was $X, now $Y", and
only moves them when the value actually changes. Run it through the Supabase
connector (`execute_sql`); in a Claude Code session with the repo, calling
`db.connection.upsert_market_price(variant_id, "Near Mint", price, source="manual_scrape")`
is equivalent.

```sql
INSERT INTO market_prices (variant_id, condition, market_price, source, updated_at)
VALUES ('<variant_id>', 'Near Mint', <price>, 'manual_scrape', NOW())
ON CONFLICT (variant_id, condition) DO UPDATE SET
  previous_market_price = CASE WHEN market_prices.market_price IS DISTINCT FROM EXCLUDED.market_price
                               THEN market_prices.market_price ELSE market_prices.previous_market_price END,
  previous_updated_at   = CASE WHEN market_prices.market_price IS DISTINCT FROM EXCLUDED.market_price
                               THEN market_prices.updated_at ELSE market_prices.previous_updated_at END,
  market_price = EXCLUDED.market_price,
  source       = EXCLUDED.source,
  updated_at   = NOW();
```

Rules:
- Condition is always `Near Mint`. Source is always `manual_scrape`.
- Write one row per variant actually read. Never write a price you could not
  match with confidence, and never write to a card the API already priced.
- This only updates market prices. Do not push anything to eBay.

## Output: exceptions only

When the run is finished, print one table and nothing else (no table of normal
prices). Include a row for each card that:

- still has **no market price** after the TCGplayer lookup,
- has **fewer than 15 listings**, or
- could not be **matched or you are not sure** about (name/number mismatch,
  duplicate candidates, variant unclear). Do not write a price for these.

| Name | Number | Printing | Market price | Listings | Reason |
|---|---|---|---|---|---|

- **Printing:** holo, non-holo, reverse holo, or the pattern name (e.g. Poké Ball reverse holo).
- **Market price:** the price read, or `none`.
- **Listings:** the count that applies (reverse holo filter count for plain reverse holos; set-page count for everything else, including pattern variants).
- **Reason:** `no price after lookup`, `fewer than 15 listings`, or `unmatched / unsure: <why>`.

If a card trips more than one reason, list the reasons together in that cell.
If there are no exceptions, say so in one line.

## Notes

- Applies to every set, not just the main ones.
- Do not push, publish, or change listings from this skill.
