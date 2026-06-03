"""
importer/ebay_auth.py — eBay token management

Handles Auth'n'Auth (User Token) for the Trading API.
Token is stored in .env as EBAY_USER_TOKEN.

Usage:
    from importer.ebay_auth import get_headers
    headers = get_headers()
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Required env vars ──────────────────────────────────────────────────────────
EBAY_APP_ID       = os.getenv("EBAY_APP_ID")
EBAY_DEV_ID       = os.getenv("EBAY_DEV_ID")
EBAY_CERT_ID      = os.getenv("EBAY_CERT_ID")
EBAY_USER_TOKEN   = os.getenv("EBAY_USER_TOKEN")
EBAY_MARKETPLACE  = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")
EBAY_SITE_ID      = "0"   # 0 = US

TRADING_API_URL   = "https://api.ebay.com/ws/api.dll"


def _check_credentials():
    missing = [k for k, v in {
        "EBAY_APP_ID":     EBAY_APP_ID,
        "EBAY_DEV_ID":     EBAY_DEV_ID,
        "EBAY_CERT_ID":    EBAY_CERT_ID,
        "EBAY_USER_TOKEN": EBAY_USER_TOKEN,
    }.items() if not v]
    if missing:
        raise EnvironmentError(
            f"Missing eBay credentials in .env: {', '.join(missing)}\n"
            "Add them and try again."
        )


def get_trading_headers(call_name: str) -> dict:
    """
    Returns the HTTP headers required for every eBay Trading API call.
    call_name examples: 'GetMyeBaySelling', 'GetOrders', 'GetItem'
    """
    _check_credentials()
    return {
        "X-EBAY-API-SITEID":        EBAY_SITE_ID,
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-CALL-NAME":     call_name,
        "X-EBAY-API-APP-NAME":      EBAY_APP_ID,
        "X-EBAY-API-DEV-NAME":      EBAY_DEV_ID,
        "X-EBAY-API-CERT-NAME":     EBAY_CERT_ID,
        "Content-Type":             "text/xml",
    }


def get_user_token() -> str:
    _check_credentials()
    return EBAY_USER_TOKEN


def verify_credentials() -> bool:
    import requests

    _check_credentials()

    if not EBAY_USER_TOKEN.startswith("v^1.1#"):
        print("⚠️  Warning: EBAY_USER_TOKEN doesn't look like a valid Auth'n'Auth token.")
        print("   It should start with 'v^1.1#...'")
        return False

    xml = """<?xml version="1.0" encoding="utf-8"?>
<GetTokenStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
</GetTokenStatusRequest>""".format(token=EBAY_USER_TOKEN)

    try:
        resp = requests.post(
            TRADING_API_URL,
            headers=get_trading_headers("GetTokenStatus"),
            data=xml.encode("utf-8"),
            timeout=10,
        )
        if resp.status_code == 200 and "Ack" in resp.text:
            if "Success" in resp.text or "Warning" in resp.text:
                print("✅ eBay credentials verified successfully.")
                return True
            elif "931" in resp.text or "Invalid" in resp.text.lower():
                print("❌ eBay token is invalid or expired.")
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
