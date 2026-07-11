"""
importer/ebay_finances.py — eBay Finances API (OAuth) connectivity

Separate auth model from ebay_auth.py's Auth'n'Auth (Trading API). The
Finances API requires an OAuth 2.0 user token with the sell.finances scope.

.env additions (per account, alongside the existing EBAY_ACCOUNT_{N}_* keys):
    EBAY_ACCOUNT_1_REFRESH_TOKEN=v^1.1#...   # ~18-month lifespan, minted once
    EBAY_ACCOUNT_1_RUNAME=Fei_Wang-...       # redirect URL name, needed only
                                              # if the refresh token is ever
                                              # re-minted via the consent flow

Access tokens (short-lived, ~2hr) are exchanged from the refresh token on
demand and cached in-process per account_num — one exchange per script run,
not per API call.

This module is deliberately minimal right now: just enough to authenticate
and make one real getTransactions call so we can see eBay's actual response
shape before building anything that writes to sale_orders. No DB writes here.
"""

import os
import json
import base64
import time
import requests
from dotenv import load_dotenv

from importer.ebay_auth import get_account_profile

load_dotenv()

OAUTH_TOKEN_URL   = "https://api.ebay.com/identity/v1/oauth2/token"
FINANCES_API_BASE = "https://apiz.ebay.com/sell/finances/v1"
FINANCES_SCOPE    = "https://api.ebay.com/oauth/api_scope/sell.finances"

# In-process cache: { account_num: (access_token, expires_at_epoch) }
_token_cache = {}


def get_finances_profile(account_num: int = 1) -> dict:
    """
    Loads the refresh token (and app/cert id, shared with the Trading API
    profile) needed to authenticate against the Finances API.
    """
    base = get_account_profile(account_num)  # reuses app_id/cert_id, raises if those are missing
    prefix = f"EBAY_ACCOUNT_{account_num}"
    refresh_token = os.getenv(f"{prefix}_REFRESH_TOKEN")
    if not refresh_token:
        raise EnvironmentError(
            f"Missing {prefix}_REFRESH_TOKEN in .env — run the OAuth consent flow "
            f"(see project notes) to mint one before using --ebay-finances-test."
        )
    return {
        "name":          base["name"],
        "app_id":        base["app_id"],
        "cert_id":       base["cert_id"],
        "refresh_token": refresh_token,
        "runame":        os.getenv(f"{prefix}_RUNAME"),  # only needed to re-mint
    }


def get_access_token(account_num: int = 1, force_refresh: bool = False) -> str:
    """
    Returns a valid Finances API access token, refreshing from the stored
    refresh token if the cached one is missing or within 60s of expiring.
    """
    cached = _token_cache.get(account_num)
    if not force_refresh and cached and cached[1] - time.time() > 60:
        return cached[0]

    profile = get_finances_profile(account_num)
    basic = base64.b64encode(
        f"{profile['app_id']}:{profile['cert_id']}".encode()
    ).decode()

    resp = requests.post(
        OAUTH_TOKEN_URL,
        headers={
            "Content-Type":  "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        data={
            "grant_type":    "refresh_token",
            "refresh_token": profile["refresh_token"],
            "scope":         ALL_SCOPES,
        },
        timeout=15,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"eBay OAuth token refresh failed ({resp.status_code}) for account "
            f"{account_num} ({profile['name']}): {resp.text[:500]}"
        )

    data = resp.json()
    access_token = data["access_token"]
    expires_in   = data.get("expires_in", 7200)
    _token_cache[account_num] = (access_token, time.time() + expires_in)

    return access_token


FULFILLMENT_API_BASE = "https://api.ebay.com/sell/fulfillment/v1"
FULFILLMENT_SCOPE     = "https://api.ebay.com/oauth/api_scope/sell.fulfillment"
ALL_SCOPES = f"{FINANCES_SCOPE} {FULFILLMENT_SCOPE}"


def test_fetch_order(order_id: str, account_num: int = 1) -> dict:
    """
    One real getOrder call (Sell Fulfillment API) with fieldGroups=TAX_BREAKDOWN.
    This is the buyer-facing side of an order — pricingSummary.priceDiscount and
    pricingSummary.deliveryCost live here, NOT in the Finances API, which only
    covers money eBay moves to/from the seller. Prints raw JSON. No DB writes.
    """
    token = get_access_token(account_num)  # same token; sell.fulfillment scope
                                            # must also be present on it — if this
                                            # 403s, the refresh token was minted
                                            # with only sell.finances and needs
                                            # to be re-consented with both scopes.

    print(f"\n🔎 Fulfillment API test — account {account_num}")
    print(f"   Fetching order {order_id}\n")

    resp = requests.get(
        f"{FULFILLMENT_API_BASE}/order/{order_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        params={"fieldGroups": "TAX_BREAKDOWN"},
        timeout=20,
    )

    print(f"HTTP {resp.status_code}")

    if resp.status_code != 200:
        print(resp.text[:1500])
        resp.raise_for_status()

    data = resp.json()
    print(json.dumps(data, indent=2))

    ps = data.get("pricingSummary", {})
    print("\n📊 pricingSummary:")
    for key in ("priceSubtotal", "priceDiscount", "deliveryCost", "deliveryDiscount",
                "tax", "fee", "adjustment", "total"):
        if key in ps:
            print(f"   {key}: {ps[key].get('value')} {ps[key].get('currency')}")

    return data


def test_fetch_transactions(order_id: str, account_num: int = 1) -> dict:
    """
    One real getTransactions call, filtered to a single order ID. Prints the
    raw JSON response and returns it. No DB writes — this is purely to see
    what eBay's fee breakdown actually looks like before building the real
    sync (specifically: does SHIPPING_LABEL reliably carry orderId, and what
    does the per-line-item FINAL_VALUE_FEE breakdown look like).
    """
    profile = get_finances_profile(account_num)
    token = get_access_token(account_num)

    print(f"\n🔎 Finances API test — account {account_num} ({profile['name']})")
    print(f"   Fetching transactions for order_id={order_id}\n")

    resp = requests.get(
        f"{FINANCES_API_BASE}/transaction",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        params={"filter": f"orderId:{{{order_id}}}", "limit": 20},
        timeout=20,
    )

    print(f"HTTP {resp.status_code}")

    if resp.status_code != 200:
        print(resp.text[:1000])
        resp.raise_for_status()

    data = resp.json()
    print(json.dumps(data, indent=2))

    tx_count = len(data.get("transactions", []))
    total    = data.get("total", tx_count)
    print(f"\n📊 {tx_count} transaction(s) returned (API reports total={total}) for order {order_id}")

    matched = 0
    for tx in data.get("transactions", []):
        tx_type = tx.get("transactionType")
        amount  = tx.get("amount", {}).get("value")
        currency = tx.get("amount", {}).get("currency")
        refs = {r.get("referenceType"): r.get("referenceId") for r in tx.get("references", [])}
        tx_order_id = tx.get("orderId") or refs.get("ORDER_ID")
        is_match = tx_order_id == order_id
        matched += is_match
        flag = "" if is_match else "  ⚠️ does NOT reference the requested order"
        print(f"   - {tx_type}: {amount} {currency} (order ref: {tx_order_id}){flag}")

    if tx_count and matched == 0:
        print(f"\n⚠️  None of the returned transactions reference order {order_id} — "
              f"the filter likely wasn't applied server-side. Treat this response as unfiltered.")
    elif matched < tx_count:
        print(f"\n⚠️  Only {matched}/{tx_count} returned transactions reference order {order_id}.")

    return data
