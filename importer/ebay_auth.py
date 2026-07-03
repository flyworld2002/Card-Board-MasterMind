"""
importer/ebay_auth.py — eBay token management

Handles Auth'n'Auth (User Token) for the Trading API.
Supports multiple eBay accounts via numbered .env entries.

.env format:
    EBAY_MARKETPLACE_ID=EBAY_US        # global, shared across accounts

    EBAY_ACCOUNT_1_NAME=flyworld2002
    EBAY_ACCOUNT_1_TOKEN=v^1.1#...
    EBAY_ACCOUNT_1_APP_ID=...
    EBAY_ACCOUNT_1_DEV_ID=...
    EBAY_ACCOUNT_1_CERT_ID=...

    # EBAY_ACCOUNT_2_NAME=flyworld_store2
    # EBAY_ACCOUNT_2_TOKEN=v^1.1#...
    # ...

Usage:
    from importer.ebay_auth import get_trading_headers, get_user_token, get_account_name
    headers = get_trading_headers("GetMyeBaySelling", account_num=1)
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Global config (shared across all accounts) ─────────────────────────────────
EBAY_MARKETPLACE  = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")
EBAY_SITE_ID      = "0"  # 0 = US — matches EBAY_US marketplace

TRADING_API_URL   = "https://api.ebay.com/ws/api.dll"


def get_account_profile(account_num: int = 1) -> dict:
    """
    Load credentials for a numbered eBay account from .env.
    Reads EBAY_ACCOUNT_{N}_NAME/TOKEN/APP_ID/DEV_ID/CERT_ID.
    Defaults to account 1.
    """
    prefix = f"EBAY_ACCOUNT_{account_num}"
    profile = {
        "name":    os.getenv(f"{prefix}_NAME",    f"account_{account_num}"),
        "token":   os.getenv(f"{prefix}_TOKEN"),
        "app_id":  os.getenv(f"{prefix}_APP_ID"),
        "dev_id":  os.getenv(f"{prefix}_DEV_ID"),
        "cert_id": os.getenv(f"{prefix}_CERT_ID"),
    }
    missing = [k for k, v in profile.items() if not v]
    if missing:
        raise EnvironmentError(
            f"Missing eBay credentials for account {account_num} in .env: {', '.join(missing)}\n"
            f"Expected keys: {prefix}_TOKEN, {prefix}_APP_ID, {prefix}_DEV_ID, {prefix}_CERT_ID"
        )
    return profile


def get_trading_headers(call_name: str, account_num: int = 1) -> dict:
    """
    Returns the HTTP headers required for every eBay Trading API call.
    call_name examples: 'GetMyeBaySelling', 'GetOrders', 'GetItem'
    """
    p = get_account_profile(account_num)
    return {
        "X-EBAY-API-SITEID":              EBAY_SITE_ID,
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-CALL-NAME":           call_name,
        "X-EBAY-API-APP-NAME":            p["app_id"],
        "X-EBAY-API-DEV-NAME":            p["dev_id"],
        "X-EBAY-API-CERT-NAME":           p["cert_id"],
        "Content-Type":                   "text/xml",
    }


def get_user_token(account_num: int = 1) -> str:
    return get_account_profile(account_num)["token"]


def get_account_name(account_num: int = 1) -> str:
    """Returns the eBay account name (e.g. 'flyworld2002') for a given account number."""
    return get_account_profile(account_num)["name"]


def verify_credentials(account_num: int = 1) -> bool:
    import requests

    p     = get_account_profile(account_num)
    token = p["token"]

    if not token.startswith("v^1.1#"):
        print(f"⚠️  Warning: token for account {account_num} ({p['name']}) doesn't look like a valid Auth'n'Auth token.")
        print("   It should start with 'v^1.1#...'")
        return False

    xml = """<?xml version="1.0" encoding="utf-8"?>
<GetTokenStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
</GetTokenStatusRequest>""".format(token=token)

    try:
        resp = requests.post(
            TRADING_API_URL,
            headers=get_trading_headers("GetTokenStatus", account_num=account_num),
            data=xml.encode("utf-8"),
            timeout=10,
        )
        if resp.status_code == 200 and "Ack" in resp.text:
            if "Success" in resp.text or "Warning" in resp.text:
                print(f"✅ eBay credentials verified for account {account_num} ({p['name']}).")
                return True
            elif "931" in resp.text or "Invalid" in resp.text.lower():
                print(f"❌ eBay token for account {account_num} ({p['name']}) is invalid or expired.")
                print("   Go to developer.ebay.com → Revoke → generate a new token.")
                return False
        if resp.status_code == 503:
            print("❌ eBay API is temporarily unavailable (503). Try again in a minute.")
        else:
            print(f"❌ Unexpected response {resp.status_code}: {resp.text[:300]}")
        return False
    except Exception as e:
        print(f"❌ Could not reach eBay API: {e}")
        return False
