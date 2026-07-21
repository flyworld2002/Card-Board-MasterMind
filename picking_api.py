r"""
picking_api.py — tiny local HTTP endpoint so the Picking tab's Refresh
button can trigger a live eBay pull from the browser.

Runs on the always-on Windows desktop, bound to the LAN. The frontend
calls it with a shared-secret header; the endpoint runs pull_picking()
(same function as `python main.py --ebay-pullpicking`) and returns the
summary. The frontend then reads the fresh snapshot from Supabase as
usual — this endpoint never serves card data itself.

.env additions:
    PICKING_API_TOKEN=<any long random string>   # required
    PICKING_API_PORT=8765                        # optional, default 8765

Run (from the project root, same venv as main.py):
    uvicorn picking_api:app --host 0.0.0.0 --port 8765

Windows one-time setup:
  1. Add PICKING_API_TOKEN to the desktop's .env (generate one, e.g.:
       python -c "import secrets; print(secrets.token_urlsafe(32))"
     — the SAME value goes into the frontend's picking.js config).
  2. Firewall rule (admin PowerShell):
       netsh advfirewall firewall add rule name="CBM Picking API" dir=in action=allow protocol=TCP localport=8765
  3. Auto-start on logon — run_picking_api.bat + Task Scheduler:
       schtasks /create /tn "CBMPickingAPI" /tr "C:\path\to\run_picking_api.bat" /sc onlogon /ru "%USERNAME%"
  4. Reserve the desktop's IP in your router so the frontend's endpoint
     URL doesn't rot.

Security model (deliberate, home-LAN appropriate): shared-secret header
over plain HTTP on a private LAN. The endpoint takes no parameters and
can only do one thing — refresh the snapshot — so the worst an attacker
on your LAN could do with the token is refresh your pick list.
"""

import os
import threading

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from importer.ebay_picking import pull_picking
from importer.ebay_pushprices import push_prices

load_dotenv()

API_TOKEN = os.getenv("PICKING_API_TOKEN")
if not API_TOKEN:
    raise EnvironmentError("PICKING_API_TOKEN missing from .env — refusing to start without auth.")

app = FastAPI(title="CBM Picking API", docs_url=None, redoc_url=None)

# The SPA is served from a different origin (Supabase-hosted / file / dev
# server), so the browser needs CORS clearance to call this. Origins are
# not a security boundary here — the token header is — so allow all.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["x-picking-token", "content-type"],
)

# One pull at a time. If a second refresh arrives while one is running,
# it waits for the lock and then runs — by that point it's a cheap,
# nearly-instant re-pull, and both callers get a complete fresh snapshot.
_pull_lock = threading.Lock()

# Separate lock for price pushes — unrelated to picking, shouldn't block on it.
_push_prices_lock = threading.Lock()


class PushPricesRequest(BaseModel):
    listing_id: str
    account_num: int = 1
    dry_run: bool = False


@app.post("/api/picking/refresh")
def refresh(x_picking_token: str = Header(default="")):
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    with _pull_lock:
        try:
            summary = pull_picking(quiet=True)
        except Exception as e:
            # Surface the real reason to the frontend banner instead of a bare 500.
            raise HTTPException(status_code=502, detail=f"pull failed: {e}")

    return summary


@app.post("/api/push-prices")
def push_prices_endpoint(body: PushPricesRequest, x_picking_token: str = Header(default="")):
    """
    Listing Pricing System (docs/plans/listing-pricing-system.md) push
    endpoint — same auth as /api/picking/refresh. Resolution always comes
    from the resolve_listing_prices() Postgres RPC; this endpoint just
    triggers the CLI's push_prices(), which diffs against pushed_*
    columns and sends only the changed variations.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    with _push_prices_lock:
        try:
            summary = push_prices(listing_id=body.listing_id, account_num=body.account_num,
                                   dry_run=body.dry_run, quiet=True)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"push failed: {e}")

    return summary


@app.get("/api/picking/health")
def health():
    """No auth — lets the frontend distinguish 'endpoint down' (show stale
    banner) from 'pull failed' (show error) without spending a pull."""
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PICKING_API_PORT", "8765")))
