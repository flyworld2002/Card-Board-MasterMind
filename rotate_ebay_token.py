"""
rotate_ebay_token.py — one-off helper to mint a NEW eBay refresh token via
the OAuth consent flow, for rotating an exposed/old one.

This is deliberately a standalone script, not wired into main.py — it's a
rare, manual, security-sensitive action, not part of normal operation.
It reuses get_account_profile()/get_finances_profile() so it doesn't need
to know your .env key names directly.

The new refresh token is printed ONLY to your own terminal — never sent
anywhere else. Copy it into .env yourself; don't paste it into chat,
Slack, or anywhere else it could leak again.

USAGE — two steps, run from the project root:

  Step 1: print the consent URL
    python3 rotate_ebay_token.py --step1 --account 1

  Open that URL in a browser, log in as the eBay seller account (BIGGYFISH),
  and approve. eBay will redirect to its own hosted result page (since the
  RuName has blank accept/decline URLs) — copy the `code=...` value straight
  out of that page's address bar. It's URL-encoded; paste it exactly as-is,
  this script decodes it for you.

  Step 2: exchange the code for a new refresh token
    python3 rotate_ebay_token.py --step2 --account 1 --code "PASTE_CODE_HERE"

  This prints the new refresh token to your terminal. Copy it into .env as
  EBAY_ACCOUNT_{N}_REFRESH_TOKEN on BOTH machines (Mac + Windows desktop —
  .env is gitignored, doesn't travel with git). Then restart anything that
  holds a cached token: picking_api.py, and any process mid-run.

  Don't forget Step 0 (do this BEFORE or AFTER, doesn't matter which):
  revoke the OLD token via My eBay -> Account -> Third-party app access,
  so the exposed one stops working regardless of what happens here.
"""

import argparse
import base64
import urllib.parse

import requests

from importer.ebay_finances import ALL_SCOPES
from importer.ebay_finances import get_finances_profile

OAUTH_AUTHORIZE_URL = "https://auth.ebay.com/oauth2/authorize"
OAUTH_TOKEN_URL      = "https://api.ebay.com/identity/v1/oauth2/token"


def step1(account_num: int):
    profile = get_finances_profile(account_num)
    if not profile.get("runame"):
        raise EnvironmentError(
            f"EBAY_ACCOUNT_{account_num}_RUNAME is missing from .env — "
            f"needed to build the consent URL. Check the developer portal "
            f"for your keyset's RuName."
        )

    params = {
        "client_id":     profile["app_id"],
        "redirect_uri":  profile["runame"],
        "response_type": "code",
        "scope":         ALL_SCOPES,
    }
    url = f"{OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    print(f"\n🔗 Consent URL for account {account_num} ({profile['name']}):\n")
    print(url)
    print(
        f"\nOpen this in a browser, log in as the eBay seller account, and "
        f"approve.\nAfter approving, copy the `code=...` value from the "
        f"resulting page's address bar,\nthen run:\n\n"
        f'  python3 rotate_ebay_token.py --step2 --account {account_num} --code "PASTE_CODE_HERE"\n'
    )


def step2(account_num: int, code: str):
    profile = get_finances_profile(account_num)
    decoded_code = urllib.parse.unquote(code)

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
            "grant_type":   "authorization_code",
            "code":         decoded_code,
            "redirect_uri": profile["runame"],
        },
        timeout=15,
    )

    if resp.status_code != 200:
        print(f"\n❌ Exchange failed ({resp.status_code}):\n{resp.text[:1000]}")
        print(
            "\nCommon causes: the code already expired (they're short-lived — "
            "run step2 promptly after approving), or it was already used once "
            "(codes are single-use)."
        )
        return

    data = resp.json()
    refresh_token = data.get("refresh_token")
    expires_in    = data.get("refresh_token_expires_in")

    print(f"\n✅ New refresh token minted for account {account_num} ({profile['name']}).")
    print(f"   Valid for ~{expires_in / 86400:.0f} days (~18 months) from now." if expires_in else "")
    print(f"\n{refresh_token}\n")
    print(
        f"Copy the value above into .env as EBAY_ACCOUNT_{account_num}_REFRESH_TOKEN "
        f"on BOTH machines (Mac + Windows desktop — .env doesn't travel with git).\n"
        f"Then restart picking_api.py and re-run any process that had the old "
        f"token cached in memory.\n\n"
        f"Don't forget to also revoke the OLD token via My eBay -> Account -> "
        f"Third-party app access, if you haven't already."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", type=int, default=1)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--step1", action="store_true", help="Print the consent URL")
    group.add_argument("--step2", action="store_true", help="Exchange a code for a new refresh token")
    parser.add_argument("--code", type=str, help="The authorization code from step1's redirect (required for --step2)")
    args = parser.parse_args()

    if args.step2 and not args.code:
        parser.error("--step2 requires --code")

    if args.step1:
        step1(args.account)
    else:
        step2(args.account, args.code)
