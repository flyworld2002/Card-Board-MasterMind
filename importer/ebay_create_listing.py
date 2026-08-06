"""
importer/ebay_create_listing.py — creates a genuinely NEW eBay listing
(AddFixedPriceItem) from a listing_templates row's queued roster, plus
the metadata steps a new listing needs first (clone from an existing
listing via GetItem, or set by hand) since nothing in this codebase has
ever had to submit that metadata before — every prior push only ever
revised a listing that already existed on eBay.

See docs/plans/listing-pricing-system.md, PLANNING session 14, item 1,
and migrations 016-018.

Every field name here (SellerProfiles/SellerPaymentProfile/
PaymentProfileID, PrimaryCategory/CategoryID, GetUserPreferences'
SupportedSellerProfile/ProfileType, etc.) was confirmed against a real
live GetItem/GetUserPreferences response before being used, not guessed.

The account's existing listings all use "PokeCards" as their
VariationSpecifics name (confirmed live) — defaulted here since a
brand-new listing has no existing VariationSpecificsSet to read one
from, but it's a discovered convention, not a hard eBay requirement, so
it's a parameter, not a constant.
"""

import json
import uuid
import xml.etree.ElementTree as ET

from db.connection import db_cursor
from importer.ebay import _post, _find, _findall, _text, NS
from importer.ebay_auth import get_user_token, get_account_name
from importer.ebay_variations_xml import (
    fetch_item, add_variation_row, insert_specifics_value, set_variation_picture,
)
from importer.ebay_listing_sync import _render_variation_name

REQUIRED_METADATA_FIELDS = [
    "category_id", "title", "description_html", "item_location",
    "item_country", "item_postal_code", "listing_duration", "condition_id",
    "payment_policy_id", "return_policy_id", "shipping_policy_id",
]

# Category-specific / not universally required by eBay — included in the
# request when set, never block "Create listing" readiness the way
# REQUIRED_METADATA_FIELDS does. sku is a genuine top-level <Item><SKU>
# (confirmed live on 336691613250, 'VarSinglesHolo' — a free-text label
# for the whole listing, not per-variation). condition_descriptor_value
# is eBay's "Card Condition" (Near Mint/Lightly Played/etc.), a field
# separate from condition_id — see CONDITION_DESCRIPTOR_VALUES below.
# item_specifics is a free-form {name: value} bag matching eBay's own
# ItemSpecifics NameValueList shape (confirmed live: Game, Set, Language,
# Manufacturer, Year Manufactured, Card Type, Country/Region of
# Manufacture, Country of Origin — plus whatever Fei adds later, e.g.
# Character).
OPTIONAL_METADATA_FIELDS = ["sku", "condition_descriptor_value", "item_specifics"]
ALL_METADATA_FIELDS = REQUIRED_METADATA_FIELDS + OPTIONAL_METADATA_FIELDS

# eBay's fixed descriptor-type ID for "Card Condition" (confirmed live) —
# only the Value varies, this Name never does.
CONDITION_DESCRIPTOR_NAME = "40001"

# Confirmed via eBay's official docs for category 183454 (Fei's real
# category, "Collectible Card Game") — see
# https://developer.ebay.com/api-docs/user-guides/static/mip-user-guide/mip-enum-condition-descriptor-ids-for-trading-cards.html
# A different category could use different values; not enforced here.
CONDITION_DESCRIPTOR_VALUES = {
    "400010": "Near mint or better",
    "400015": "Lightly played (Excellent)",
    "400016": "Moderately played (Very good)",
    "400017": "Heavily played (Poor)",
}

DEFAULT_SPECIFIC_NAME = "PokeCards"


# ══════════════════════════════════════════════════════════════════════════════
# Business policies (for the manual-entry picker)
# ══════════════════════════════════════════════════════════════════════════════

def list_business_policies(account_num: int = 1) -> dict:
    """Returns {'payment': [...], 'return': [...], 'shipping': [...]}, each
    a list of {profile_id, profile_name}, from the account's configured
    eBay Business Policies (GetUserPreferences,
    ShowSellerProfilePreferences=true)."""
    token = get_user_token(account_num)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetUserPreferencesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  <ShowSellerProfilePreferences>true</ShowSellerProfilePreferences>
</GetUserPreferencesRequest>"""
    root = _post("GetUserPreferences", xml, account_num=account_num)

    result = {"payment": [], "return": [], "shipping": []}
    spp = _find(root, "SellerProfilePreferences")
    if spp is None:
        return result
    supported = _find(spp, "SupportedSellerProfiles")
    if supported is None:
        return result

    TYPE_MAP = {"PAYMENT": "payment", "RETURN_POLICY": "return", "SHIPPING": "shipping"}
    for profile in _findall(supported, "SupportedSellerProfile"):
        bucket = TYPE_MAP.get(_text(profile, "ProfileType"))
        if not bucket:
            continue
        result[bucket].append({
            "profile_id": _text(profile, "ProfileID"),
            "profile_name": _text(profile, "ProfileName"),
        })
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Listing-level metadata — clone from an existing listing, or set by hand
# ══════════════════════════════════════════════════════════════════════════════

def fetch_listing_metadata(item_id: str, account_num: int = 1) -> dict:
    """Reads the listing-level (non-variation) metadata off a real,
    already-live listing via GetItem — everything a new listing's
    AddFixedPriceItem call needs besides its own cards."""
    item = fetch_item(item_id, account_num=account_num)

    category = _find(item, "PrimaryCategory")
    seller_profiles = _find(item, "SellerProfiles")

    def _profile_id(parent_tag, id_tag):
        if seller_profiles is None:
            return None
        node = _find(seller_profiles, parent_tag)
        return _text(node, id_tag) if node is not None else None

    condition_descriptor_value = None
    descriptors = _find(item, "ConditionDescriptors")
    if descriptors is not None:
        for d in _findall(descriptors, "ConditionDescriptor"):
            if _text(d, "Name") == CONDITION_DESCRIPTOR_NAME:
                condition_descriptor_value = _text(d, "Value")
                break

    item_specifics = {}
    specifics = _find(item, "ItemSpecifics")
    if specifics is not None:
        for nvl in _findall(specifics, "NameValueList"):
            name = _text(nvl, "Name")
            value = _text(nvl, "Value")
            if name:
                item_specifics[name] = value

    return {
        "category_id":        _text(category, "CategoryID") if category is not None else None,
        "title":              _text(item, "Title"),
        "description_html":   _text(item, "Description"),
        "item_location":      _text(item, "Location"),
        "item_country":       _text(item, "Country"),
        "item_postal_code":   _text(item, "PostalCode"),
        "listing_duration":   _text(item, "ListingDuration"),
        "condition_id":       _text(item, "ConditionID"),
        "payment_policy_id":  _profile_id("SellerPaymentProfile", "PaymentProfileID"),
        "return_policy_id":   _profile_id("SellerReturnProfile", "ReturnProfileID"),
        "shipping_policy_id": _profile_id("SellerShippingProfile", "ShippingProfileID"),
        "sku":                       _text(item, "SKU"),
        "condition_descriptor_value": condition_descriptor_value,
        "item_specifics":            item_specifics,
    }


def clone_listing_metadata(template_id: str, source_listing_id: str, account_num: int = 1) -> dict:
    """Fetches metadata from a real existing listing and writes it onto
    template_id's listing_templates row (source='cloned'). Fei can review/
    edit any field afterward before the actual create push — this is a
    snapshot taken now, not re-fetched live at create time."""
    fields = fetch_listing_metadata(source_listing_id, account_num=account_num)
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE listing_templates
            SET source = 'cloned', cloned_from_listing_id = %s,
                category_id = %s, title = %s, description_html = %s,
                item_location = %s, item_country = %s, item_postal_code = %s,
                listing_duration = %s, condition_id = %s,
                payment_policy_id = %s, return_policy_id = %s, shipping_policy_id = %s,
                sku = %s, condition_descriptor_value = %s, item_specifics = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (source_listing_id, fields["category_id"], fields["title"], fields["description_html"],
             fields["item_location"], fields["item_country"], fields["item_postal_code"],
             fields["listing_duration"], fields["condition_id"],
             fields["payment_policy_id"], fields["return_policy_id"], fields["shipping_policy_id"],
             fields["sku"], fields["condition_descriptor_value"], json.dumps(fields["item_specifics"]),
             template_id),
        )
    return {"template_id": template_id, "cloned_from": source_listing_id, **fields}


def set_manual_listing_metadata(template_id: str, **fields) -> dict:
    """Writes listing-level metadata by hand (source='manual') — same
    columns clone_listing_metadata() writes (required + optional/
    category-specific ones), just typed in instead of copied from a live
    listing. Unknown kwargs are ignored; omitted fields are left
    untouched (partial updates allowed, e.g. filling in one missing
    field at a time)."""
    cols = [f for f in ALL_METADATA_FIELDS if f in fields]
    if not cols:
        return {"template_id": template_id, "updated": []}
    set_clause = ", ".join(f"{c} = %s" for c in cols)
    values = [json.dumps(fields[c]) if c == "item_specifics" else fields[c] for c in cols]
    with db_cursor() as cur:
        cur.execute(
            f"UPDATE listing_templates SET source = 'manual', {set_clause}, updated_at = now() "
            f"WHERE id = %s",
            values + [template_id],
        )
    return {"template_id": template_id, "updated": cols}


# ══════════════════════════════════════════════════════════════════════════════
# Readiness preview
# ══════════════════════════════════════════════════════════════════════════════

def _load_template(cur, template_id: str) -> dict | None:
    cur.execute("SELECT * FROM listing_templates WHERE id = %s", (template_id,))
    return cur.fetchone()


def preview_new_listing(template_id: str, account_num: int = 1, platform: str = "ebay") -> dict:
    """Readiness check for creating a brand-new listing from template_id's
    queued roster — same dry-run-first pattern every other push in this
    feature already uses. Returns which queued rows are ready to go live
    now vs. not (and why), plus which required metadata fields are still
    missing. Never touches eBay — pure DB read via resolve_listing_prices'
    p_template_id path (migration 016)."""
    with db_cursor() as cur:
        template = _load_template(cur, template_id)
        if template is None:
            return {"error": "no such template"}
        if template["listing_id"]:
            return {"error": f"template already has a live listing_id ({template['listing_id']}) — "
                              f"use --ebay-pushprices instead, not create-listing"}

        missing_metadata = [f for f in REQUIRED_METADATA_FIELDS if not template.get(f)]

        cur.execute(
            "SELECT * FROM resolve_listing_prices(%s, NULL, %s)",
            (platform, template_id),
        )
        resolved = cur.fetchall()

    ready, not_ready = [], []
    for r in resolved:
        if r["status"] != "queued":
            continue  # shouldn't happen for a listing with no listing_id yet, but be safe
        if not r["available_qty"]:
            not_ready.append({**r, "reason": "0 available quantity"})
        else:
            ready.append(r)

    return {
        "template_id": template_id,
        "template_name": template["name"],
        "missing_metadata": missing_metadata,
        "ready": ready,
        "not_ready": not_ready,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Create the listing
# ══════════════════════════════════════════════════════════════════════════════

def _build_add_item_xml(template: dict, variations: ET.Element, total_qty: int,
                         min_price: float, account_num: int, dry_run: bool = False) -> str:
    """Builds the full AddFixedPriceItem request. Free-text fields
    (Title/Description) go through ElementTree's own text-node
    serialization so `&`/`<`/`>` in a real HTML description get escaped
    correctly — unlike every other XML builder in this codebase, this is
    the first one carrying genuinely arbitrary text, not just numbers/IDs.

    dry_run=True substitutes a placeholder for the real auth token —
    this is the first push in this feature whose dry-run output returns
    the full request XML (every earlier one only ever printed a summary),
    so a real Auth'n'Auth token must never end up embedded in something
    that could be logged, displayed in a future web UI, or pasted
    somewhere — only a real send needs the real token."""
    token = "***DRY-RUN-TOKEN-REDACTED***" if dry_run else get_user_token(account_num)

    item = ET.Element(f"{{{NS}}}Item")

    def add(tag, text):
        el = ET.SubElement(item, f"{{{NS}}}{tag}")
        el.text = text
        return el

    add("Title", template["title"])
    add("Description", template["description_html"])

    category = ET.SubElement(item, f"{{{NS}}}PrimaryCategory")
    ET.SubElement(category, f"{{{NS}}}CategoryID").text = template["category_id"]

    add("ListingType", "FixedPriceItem")
    add("ListingDuration", template["listing_duration"])
    add("Currency", "USD")
    add("Country", template["item_country"])
    add("Location", template["item_location"])
    add("PostalCode", template["item_postal_code"])
    add("ConditionID", template["condition_id"])
    # Top-level Quantity/StartPrice are required by eBay's schema even on a
    # variation listing (confirmed live — an existing variation listing
    # still carries both) even though the real per-card values live on
    # each <Variation> — a reasonable summary, not otherwise consumed.
    add("Quantity", str(total_qty))
    add("StartPrice", f"{min_price:.2f}")
    add("SKU", template.get("sku"))

    if template.get("condition_descriptor_value"):
        descriptors = ET.SubElement(item, f"{{{NS}}}ConditionDescriptors")
        descriptor = ET.SubElement(descriptors, f"{{{NS}}}ConditionDescriptor")
        ET.SubElement(descriptor, f"{{{NS}}}Name").text = CONDITION_DESCRIPTOR_NAME
        ET.SubElement(descriptor, f"{{{NS}}}Value").text = template["condition_descriptor_value"]

    item_specifics = template.get("item_specifics") or {}
    if item_specifics:
        specifics_node = ET.SubElement(item, f"{{{NS}}}ItemSpecifics")
        for name, value in item_specifics.items():
            if not value:
                continue
            nvl = ET.SubElement(specifics_node, f"{{{NS}}}NameValueList")
            ET.SubElement(nvl, f"{{{NS}}}Name").text = name
            ET.SubElement(nvl, f"{{{NS}}}Value").text = value

    seller_profiles = ET.SubElement(item, f"{{{NS}}}SellerProfiles")
    payment = ET.SubElement(seller_profiles, f"{{{NS}}}SellerPaymentProfile")
    ET.SubElement(payment, f"{{{NS}}}PaymentProfileID").text = template["payment_policy_id"]
    ret = ET.SubElement(seller_profiles, f"{{{NS}}}SellerReturnProfile")
    ET.SubElement(ret, f"{{{NS}}}ReturnProfileID").text = template["return_policy_id"]
    ship = ET.SubElement(seller_profiles, f"{{{NS}}}SellerShippingProfile")
    ET.SubElement(ship, f"{{{NS}}}ShippingProfileID").text = template["shipping_policy_id"]

    item.append(variations)

    ET.register_namespace("", NS)
    item_xml = ET.tostring(item, encoding="unicode")

    return f"""<?xml version="1.0" encoding="utf-8"?>
<AddFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  {item_xml}
</AddFixedPriceItemRequest>"""


def create_listing(template_id: str, account_num: int = 1, platform: str = "ebay",
                    dry_run: bool = False, quiet: bool = False,
                    specific_name: str = DEFAULT_SPECIFIC_NAME) -> dict:
    """
    Creates a genuinely new eBay listing (AddFixedPriceItem) from
    template_id's ready queued roster rows in one batch. Refuses if the
    template already has a live listing_id (use --ebay-pushprices
    instead) or is missing required metadata (clone or set it first).
    Rows with 0 available quantity are excluded and stay queued — same
    "skip, don't block the batch" behavior _do_promotions() already uses
    elsewhere, confirmed with Fei for this feature too.

    --dry-run builds and returns the full request without ever sending
    it to eBay. The DB is only written after a confirmed successful
    AddFixedPriceItem response (new listing_id, platform_listings rows,
    roster status, ebay_listing_map) — same "never write before a
    confirmed eBay success" rule push_prices()/_do_promotions() already
    follow, applied here to a first-ever create instead of a revise.
    """
    def p(msg):
        if not quiet:
            print(msg)

    with db_cursor() as cur:
        template = _load_template(cur, template_id)
        if template is None:
            return {"template_id": template_id, "created": False, "dry_run": dry_run,
                     "error": "no such template"}
        if template["listing_id"]:
            return {"template_id": template_id, "created": False, "dry_run": dry_run,
                     "error": f"template already has a live listing_id ({template['listing_id']}) — "
                              f"use --ebay-pushprices instead, not create-listing"}

        missing_metadata = [f for f in REQUIRED_METADATA_FIELDS if not template.get(f)]
        if missing_metadata:
            return {"template_id": template_id, "created": False, "dry_run": dry_run,
                     "error": f"missing required metadata: {', '.join(missing_metadata)} — "
                              f"clone from an existing listing or set it manually first"}

        cur.execute("SELECT * FROM resolve_listing_prices(%s, NULL, %s)", (platform, template_id))
        resolved = cur.fetchall()

        ready = [dict(r) for r in resolved if r["status"] == "queued" and r["available_qty"]]
        not_ready = [r for r in resolved if r["status"] == "queued" and not r["available_qty"]]

        if not ready:
            return {"template_id": template_id, "created": False, "dry_run": dry_run,
                     "error": f"no ready cards to create a listing with "
                              f"({len(not_ready)} queued but 0 available quantity)"}

        for r in not_ready:
            p(f"  [NOT READY] {r['card_name']} #{r['card_number']}: 0 available quantity — "
              f"will stay queued")

        # Build the VariationSpecificsSet + one <Variation> per ready row.
        variations = ET.Element(f"{{{NS}}}Variations")
        for r in ready:
            promoted_name = r["custom_name"] or _render_variation_name(cur, r["variant_id"], template_id)
            insert_specifics_value(variations, specific_name, promoted_name, position=None)
            add_variation_row(variations, {specific_name: promoted_name},
                               quantity=r["available_qty"], start_price=float(r["resolved_price"]))
            r["_promoted_name"] = promoted_name

        # Pictures only after every add_variation_row() call, one pass —
        # same rule set_variation_picture()'s docstring requires.
        for r in ready:
            if r["eps_picture_url"]:
                set_variation_picture(variations, r["_promoted_name"], r["eps_picture_url"])

        total_qty = sum(r["available_qty"] for r in ready)
        min_price = min(float(r["resolved_price"]) for r in ready)
        xml = _build_add_item_xml(template, variations, total_qty, min_price, account_num,
                                   dry_run=dry_run)

        if dry_run:
            p(f"[DRY-RUN] would create a new listing with {len(ready)} card(s), "
              f"{len(not_ready)} left queued (0 qty)")
            return {"template_id": template_id, "created": False, "dry_run": True,
                     "ready_count": len(ready), "not_ready_count": len(not_ready), "xml": xml}

        root = _post("AddFixedPriceItem", xml, account_num=account_num)
        new_item_id = _text(root, "ItemID")
        if not new_item_id:
            return {"template_id": template_id, "created": False, "dry_run": False,
                     "error": "AddFixedPriceItem succeeded but returned no ItemID — "
                              "check the account's Seller Hub before retrying"}

        account = get_account_name(account_num)
        cur.execute(
            "UPDATE listing_templates SET listing_id = %s, updated_at = now() WHERE id = %s",
            (new_item_id, template_id),
        )
        for r in ready:
            new_platform_listing_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO platform_listings
                    (id, platform, account, listing_id, external_id, variant_id, list_price,
                     quantity_listed, status, sync_enabled, template_id, listed_at,
                     pushed_price, pushed_qty, pushed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', true, %s, now(), %s, %s, now())
                """,
                (new_platform_listing_id, platform, account, new_item_id, r["_promoted_name"],
                 r["variant_id"], float(r["resolved_price"]), r["available_qty"], template_id,
                 float(r["resolved_price"]), r["available_qty"]),
            )
            cur.execute(
                "UPDATE listing_card_assignments SET status = 'active', platform_listing_id = %s, "
                "updated_at = now() WHERE id = %s",
                (new_platform_listing_id, r["row_id"]),
            )
            cur.execute(
                """
                INSERT INTO ebay_listing_map
                    (item_id, listing_id, variation_name, variant_id, condition, source, last_synced_at)
                VALUES (%s, %s, %s, %s, 'Near Mint', 'push', now())
                ON CONFLICT (item_id, variation_name) DO UPDATE
                    SET variant_id = EXCLUDED.variant_id, source = EXCLUDED.source,
                        last_synced_at = now()
                """,
                (new_item_id, new_item_id, r["_promoted_name"], r["variant_id"]),
            )

    p(f"[{new_item_id}] created new listing with {len(ready)} card(s) ({len(not_ready)} left queued).")
    return {"template_id": template_id, "created": True, "dry_run": False,
             "listing_id": new_item_id, "ready_count": len(ready), "not_ready_count": len(not_ready)}


# ══════════════════════════════════════════════════════════════════════════════
# Revise metadata on an ALREADY-LIVE listing — the mirror image of
# create_listing()'s metadata step, for after the fact instead of before.
# ══════════════════════════════════════════════════════════════════════════════

def revise_listing_metadata(template_id: str, account_num: int = 1, dry_run: bool = False,
                             **fields) -> dict:
    """
    Revises listing-level metadata (title/description/category/location/
    business policies/etc.) on a listing that's ALREADY live, via
    ReviseFixedPriceItem — only top-level <Item> fields, no <Variations>
    touched at all. Unlike create_listing()'s <Variations> block, eBay
    only requires sending the fields you're actually changing here — no
    deep-copy-and-resend needed for a plain top-level revise.

    `fields` is a partial set of REQUIRED_METADATA_FIELDS keys (only
    what the caller actually wants to change) — merged with the
    template's current values so every revise call is a complete,
    consistent request even though the caller only supplied a diff.

    Not every field is necessarily revisable once a listing has real
    activity — eBay commonly restricts ListingDuration and ConditionID
    changes post-listing, for example. This doesn't pre-validate that;
    a rejection surfaces as a normal eBay error via _post(), same as any
    other unsupported revise elsewhere in this codebase.

    DB (listing_templates) is only updated after a confirmed successful
    revise — same "never write before a confirmed eBay success" rule
    every other push in this feature follows.
    """
    with db_cursor() as cur:
        template = _load_template(cur, template_id)
        if template is None:
            return {"template_id": template_id, "revised": False, "dry_run": dry_run,
                     "error": "no such template"}
        if not template["listing_id"]:
            return {"template_id": template_id, "revised": False, "dry_run": dry_run,
                     "error": "template has no live listing_id yet — use create_listing() instead"}

        changed_cols = [k for k in ALL_METADATA_FIELDS if k in fields]
        if not changed_cols:
            return {"template_id": template_id, "revised": False, "dry_run": dry_run,
                     "error": "no metadata fields provided to revise"}

        merged = {k: fields.get(k, template.get(k)) for k in ALL_METADATA_FIELDS}

        token = "***DRY-RUN-TOKEN-REDACTED***" if dry_run else get_user_token(account_num)

        item = ET.Element(f"{{{NS}}}Item")
        ET.SubElement(item, f"{{{NS}}}ItemID").text = template["listing_id"]

        def add(tag, text):
            if text is None:
                return
            el = ET.SubElement(item, f"{{{NS}}}{tag}")
            el.text = text

        add("Title", merged["title"])
        add("Description", merged["description_html"])
        if merged["category_id"]:
            category = ET.SubElement(item, f"{{{NS}}}PrimaryCategory")
            ET.SubElement(category, f"{{{NS}}}CategoryID").text = merged["category_id"]
        add("Country", merged["item_country"])
        add("Location", merged["item_location"])
        add("PostalCode", merged["item_postal_code"])
        add("ConditionID", merged["condition_id"])
        add("ListingDuration", merged["listing_duration"])
        add("SKU", merged["sku"])

        if merged["condition_descriptor_value"]:
            descriptors = ET.SubElement(item, f"{{{NS}}}ConditionDescriptors")
            descriptor = ET.SubElement(descriptors, f"{{{NS}}}ConditionDescriptor")
            ET.SubElement(descriptor, f"{{{NS}}}Name").text = CONDITION_DESCRIPTOR_NAME
            ET.SubElement(descriptor, f"{{{NS}}}Value").text = merged["condition_descriptor_value"]

        if merged["item_specifics"]:
            specifics_node = ET.SubElement(item, f"{{{NS}}}ItemSpecifics")
            for name, value in merged["item_specifics"].items():
                if not value:
                    continue
                nvl = ET.SubElement(specifics_node, f"{{{NS}}}NameValueList")
                ET.SubElement(nvl, f"{{{NS}}}Name").text = name
                ET.SubElement(nvl, f"{{{NS}}}Value").text = value

        policy_fields = ("payment_policy_id", "return_policy_id", "shipping_policy_id")
        if any(merged[k] for k in policy_fields):
            seller_profiles = ET.SubElement(item, f"{{{NS}}}SellerProfiles")
            if merged["payment_policy_id"]:
                node = ET.SubElement(seller_profiles, f"{{{NS}}}SellerPaymentProfile")
                ET.SubElement(node, f"{{{NS}}}PaymentProfileID").text = merged["payment_policy_id"]
            if merged["return_policy_id"]:
                node = ET.SubElement(seller_profiles, f"{{{NS}}}SellerReturnProfile")
                ET.SubElement(node, f"{{{NS}}}ReturnProfileID").text = merged["return_policy_id"]
            if merged["shipping_policy_id"]:
                node = ET.SubElement(seller_profiles, f"{{{NS}}}SellerShippingProfile")
                ET.SubElement(node, f"{{{NS}}}ShippingProfileID").text = merged["shipping_policy_id"]

        ET.register_namespace("", NS)
        item_xml = ET.tostring(item, encoding="unicode")
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  {item_xml}
</ReviseFixedPriceItemRequest>"""

        if dry_run:
            return {"template_id": template_id, "revised": False, "dry_run": True,
                     "listing_id": template["listing_id"], "xml": xml}

        _post("ReviseFixedPriceItem", xml, account_num=account_num)

        set_clause = ", ".join(f"{c} = %s" for c in changed_cols)
        values = [json.dumps(merged[c]) if c == "item_specifics" else merged[c] for c in changed_cols]
        cur.execute(
            f"UPDATE listing_templates SET {set_clause}, updated_at = now() WHERE id = %s",
            values + [template_id],
        )

    return {"template_id": template_id, "revised": True, "dry_run": False,
             "listing_id": template["listing_id"], "fields": changed_cols}
