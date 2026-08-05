r"""
picking_api.py — tiny local HTTP endpoint so the web frontend can
trigger live eBay actions (picking pull, price/qty pushes, market price
refresh jobs) from the browser.

Runs on the always-on Windows desktop. As of 2026-08, reachable over
Tailscale (not just the home LAN) at the desktop's stable Tailscale
hostname, and served over HTTPS using a Tailscale-issued cert — the
frontend itself is now hosted on Cloudflare Pages over HTTPS
(https://card-board-mastermind-webinvmanagement.pages.dev), and browsers
block an HTTPS page from calling a plain-HTTP endpoint (mixed content),
so this can no longer serve plain HTTP. The frontend calls it with a
shared-secret header; each endpoint runs the corresponding importer
function and returns a summary — this service never serves card data
itself, everything else still goes through Supabase directly.

.env additions:
    PICKING_API_TOKEN=<any long random string>   # required
    PICKING_API_PORT=8765                        # optional, default 8765
    PICKING_API_SSL_CERTFILE=<path to .crt>      # optional, only used by
    PICKING_API_SSL_KEYFILE=<path to .key>       # `python picking_api.py` directly —
                                                  # the real scheduled-task launch
                                                  # (run_picking_api.bat) passes
                                                  # --ssl-certfile/--ssl-keyfile on
                                                  # the uvicorn command line instead.

Run (from the project root, same venv as main.py):
    uvicorn picking_api:app --host 0.0.0.0 --port 8765 \
        --ssl-certfile desktop-tu1m2fc.tail2c58d7.ts.net.crt \
        --ssl-keyfile desktop-tu1m2fc.tail2c58d7.ts.net.key

Windows one-time setup:
  1. Add PICKING_API_TOKEN to the desktop's .env (generate one, e.g.:
       python -c "import secrets; print(secrets.token_urlsafe(32))"
     — the SAME value goes into the frontend's PICKING_API_TOKEN config).
  2. Firewall rule (admin PowerShell):
       netsh advfirewall firewall add rule name="CBM Picking API" dir=in action=allow protocol=TCP localport=8765
  3. Auto-start on logon — run_picking_api.bat + Task Scheduler:
       schtasks /create /tn "CBMPickingAPI" /tr "C:\path\to\run_picking_api.bat" /sc onlogon /ru "%USERNAME%"
  4. Install Tailscale on the desktop (`winget install Tailscale.Tailscale`),
     log in, enable "HTTPS Certificates" for the tailnet at
     https://login.tailscale.com/admin/dns, then provision the cert:
       tailscale cert desktop-tu1m2fc.tail2c58d7.ts.net
     Re-run that command to renew if the cert ever expires (Tailscale
     auto-renews in the background while tailscaled is running, so this
     should rarely be needed by hand). NEVER commit the resulting .crt/.key
     files — *.crt and *.key are gitignored project-wide for this reason.

Security model: shared-secret header over HTTPS on a private Tailscale
network (not the public internet) — only devices logged into this
specific tailnet can even reach the hostname at all. The endpoints do
real writes now (price/qty pushes, market price refresh), so this is a
step up from the original "read-only refresh" threat model the shared-
secret-only design first assumed — worth keeping in mind if more
destructive actions get added later.
"""

import os
import tempfile
import threading
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from importer.ebay_picking import pull_picking
from importer.ebay_pushprices import (
    push_prices, push_single_card_live, remove_single_card_live, stage_card_picture,
    revise_single_variation_qty,
)
from importer.ebay_create_listing import (
    list_business_policies, clone_listing_metadata, set_manual_listing_metadata,
    revise_listing_metadata, preview_new_listing, create_listing, REQUIRED_METADATA_FIELDS,
)
from importer.job_runner import start_job, get_job, list_jobs
from importer.market_price_refresh import refresh_market_prices
from importer.excel_staging import import_from_excel

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
    allow_methods=["GET", "POST"],
    allow_headers=["x-picking-token", "content-type"],
)

# One pull at a time. If a second refresh arrives while one is running,
# it waits for the lock and then runs — by that point it's a cheap,
# nearly-instant re-pull, and both callers get a complete fresh snapshot.
_pull_lock = threading.Lock()

# Separate lock for price pushes — unrelated to picking, shouldn't block on it.
_push_prices_lock = threading.Lock()

# Separate again from the general price push — a single-card push touches
# one specific queued row and shouldn't queue behind a full-listing push
# (or vice versa) any longer than it has to.
_push_card_lock = threading.Lock()

# Same reasoning as _push_card_lock — remove is its own action, shouldn't
# queue behind a push and vice versa.
_remove_card_lock = threading.Lock()

# Same reasoning again — staging a picture (an EPS upload) shouldn't
# queue behind any of the other actions.
_stage_picture_lock = threading.Lock()

# Balance Qty fires several of these in a row (one per listing being
# rebalanced) — its own lock so it doesn't contend with unrelated pushes,
# but each individual revise call still serializes against any other
# concurrent qty revision.
_revise_qty_lock = threading.Lock()

# Creating a brand-new listing (AddFixedPriceItem) is its own action —
# own lock, same reasoning as every other write endpoint here.
_create_listing_lock = threading.Lock()

# Revising an already-live listing's metadata — own lock, same reasoning.
_revise_metadata_lock = threading.Lock()


class PushPricesRequest(BaseModel):
    listing_id: str
    account_num: int = 1
    dry_run: bool = False


class PushCardRequest(BaseModel):
    row_id: str
    account_num: int = 1
    dry_run: bool = False


class RemoveCardRequest(BaseModel):
    row_id: str
    account_num: int = 1
    dry_run: bool = False


class StagePictureRequest(BaseModel):
    row_id: str
    image_url: str
    account_num: int = 1


class ReviseQtyRequest(BaseModel):
    platform_listing_id: str
    new_qty: int
    account_num: int = 1
    dry_run: bool = False


class MarketPriceRefreshRequest(BaseModel):
    set_name: str | None = None
    card_id: str | None = None


class CloneListingMetadataRequest(BaseModel):
    template_id: str
    source_listing_id: str
    account_num: int = 1


class SetListingMetadataRequest(BaseModel):
    template_id: str
    category_id: str | None = None
    title: str | None = None
    description_html: str | None = None
    item_location: str | None = None
    item_country: str | None = None
    item_postal_code: str | None = None
    listing_duration: str | None = None
    condition_id: str | None = None
    payment_policy_id: str | None = None
    return_policy_id: str | None = None
    shipping_policy_id: str | None = None


class ReviseListingMetadataRequest(BaseModel):
    template_id: str
    account_num: int = 1
    dry_run: bool = False
    category_id: str | None = None
    title: str | None = None
    description_html: str | None = None
    item_location: str | None = None
    item_country: str | None = None
    item_postal_code: str | None = None
    listing_duration: str | None = None
    condition_id: str | None = None
    payment_policy_id: str | None = None
    return_policy_id: str | None = None
    shipping_policy_id: str | None = None


class CreateListingRequest(BaseModel):
    template_id: str
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


@app.post("/api/push-card")
def push_card_endpoint(body: PushCardRequest, x_picking_token: str = Header(default="")):
    """
    Pushes ONE queued roster row (listing_card_assignments.id) live as a
    brand-new variation on its listing — does not touch any other
    variation's price/qty. Same auth as /api/push-prices, separate lock.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    with _push_card_lock:
        try:
            result = push_single_card_live(row_id=body.row_id, account_num=body.account_num,
                                            dry_run=body.dry_run, quiet=True)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"push failed: {e}")

    return result


@app.post("/api/remove-card")
def remove_card_endpoint(body: RemoveCardRequest, x_picking_token: str = Header(default="")):
    """
    Pulls ONE active roster row's variation off its live listing — the
    reverse of /api/push-card. Roster row goes back to 'queued', not
    deleted. Same auth as the other push/remove endpoints, own lock.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    with _remove_card_lock:
        try:
            result = remove_single_card_live(row_id=body.row_id, account_num=body.account_num,
                                              dry_run=body.dry_run, quiet=True)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"remove failed: {e}")

    return result


@app.post("/api/stage-card-picture")
def stage_card_picture_endpoint(body: StagePictureRequest, x_picking_token: str = Header(default="")):
    """
    Uploads body.image_url to eBay's own hosting (EPS) right now and
    stages the resulting URL on a QUEUED roster row — attached
    automatically the next time that row actually gets pushed live.
    No R2/card_master involvement — this only ever touches eBay's own
    image hosting. Same auth as the other endpoints, own lock.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    with _stage_picture_lock:
        try:
            result = stage_card_picture(row_id=body.row_id, source_url=body.image_url,
                                         account_num=body.account_num, quiet=True)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"picture upload failed: {e}")

    return result


@app.post("/api/stage-card-picture-file")
async def stage_card_picture_file_endpoint(
    row_id: str = Form(...),
    account_num: int = Form(1),
    file: UploadFile = File(...),
    x_picking_token: str = Header(default=""),
):
    """
    Same as /api/stage-card-picture but for a directly-uploaded local
    file instead of a URL — a separate route because FastAPI can't mix a
    JSON body with multipart Form/File params on one endpoint. Uploads
    file bytes straight to EPS, no download step.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    image_bytes = await file.read()

    with _stage_picture_lock:
        try:
            result = stage_card_picture(row_id=row_id, image_bytes=image_bytes,
                                         filename=file.filename or "card.jpg",
                                         account_num=account_num, quiet=True)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"picture upload failed: {e}")

    return result


@app.post("/api/revise-variation-qty")
def revise_variation_qty_endpoint(body: ReviseQtyRequest, x_picking_token: str = Header(default="")):
    """
    Directly revises ONE existing live variation's quantity, no template
    required — built for "Balance Qty" (redistributing a card's shared
    inventory across every listing that currently offers it, including
    ones never onboarded into a listing_templates row). Same auth as the
    other endpoints, own lock.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    with _revise_qty_lock:
        try:
            result = revise_single_variation_qty(platform_listing_id=body.platform_listing_id,
                                                  new_qty=body.new_qty, account_num=body.account_num,
                                                  dry_run=body.dry_run, quiet=True)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"revise failed: {e}")

    return result


@app.get("/api/business-policies")
def business_policies_endpoint(account_num: int = 1, x_picking_token: str = Header(default="")):
    """
    Lists the account's configured eBay Business Policies (payment/return/
    shipping profile IDs) via GetUserPreferences — for the manual listing-
    metadata entry path. Read-only against eBay, no lock needed.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    try:
        return list_business_policies(account_num=account_num)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: {e}")


@app.post("/api/listing-metadata/clone")
def clone_listing_metadata_endpoint(body: CloneListingMetadataRequest,
                                     x_picking_token: str = Header(default="")):
    """
    Copies listing-level metadata (category, description, location,
    duration, business policies) from an existing live listing onto a
    template that doesn't have a listing_id yet, via GetItem. Read-only
    against eBay; writes only listing_templates. No lock — a plain DB
    update after one read, same risk profile as any other Configuration
    edit elsewhere in this app.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    try:
        return clone_listing_metadata(template_id=body.template_id,
                                       source_listing_id=body.source_listing_id,
                                       account_num=body.account_num)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"clone failed: {e}")


@app.post("/api/listing-metadata/manual")
def set_listing_metadata_endpoint(body: SetListingMetadataRequest,
                                   x_picking_token: str = Header(default="")):
    """Sets listing-level metadata by hand instead of cloning — same
    columns the clone endpoint writes. No eBay call, partial updates
    allowed (only non-null fields in the request are written)."""
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    fields = {k: v for k, v in body.model_dump().items()
              if k in REQUIRED_METADATA_FIELDS and v is not None}
    return set_manual_listing_metadata(template_id=body.template_id, **fields)


@app.post("/api/listing-metadata/revise")
def revise_listing_metadata_endpoint(body: ReviseListingMetadataRequest,
                                      x_picking_token: str = Header(default="")):
    """
    Revises listing-level metadata on a listing that's ALREADY LIVE, via
    ReviseFixedPriceItem — the mirror image of /api/listing-metadata/manual
    for after the fact instead of before creation. Own lock, same
    reasoning as every other write endpoint here. body.dry_run builds and
    returns the request without sending it.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    fields = {k: v for k, v in body.model_dump().items()
              if k in REQUIRED_METADATA_FIELDS and v is not None}

    with _revise_metadata_lock:
        try:
            result = revise_listing_metadata(template_id=body.template_id, account_num=body.account_num,
                                              dry_run=body.dry_run, **fields)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"revise failed: {e}")

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/api/preview-new-listing/{template_id}")
def preview_new_listing_endpoint(template_id: str, account_num: int = 1,
                                  x_picking_token: str = Header(default="")):
    """
    Which of a template's queued cards are ready to go into a brand-new
    listing right now vs. not (and why), plus any missing required
    metadata. Read-only, no eBay call — pure DB read via
    resolve_listing_prices()'s p_template_id path (migration 016).
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    result = preview_new_listing(template_id=template_id, account_num=account_num)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/create-listing")
def create_listing_endpoint(body: CreateListingRequest, x_picking_token: str = Header(default="")):
    """
    Creates a genuinely NEW eBay listing (AddFixedPriceItem) from a
    template's ready queued roster in one batch — the first-ever write
    to eBay for a listing that doesn't exist yet, as opposed to every
    other endpoint in this file, which only ever revises one that
    already does. Own lock, same reasoning as every other write endpoint
    here. --dry-run (body.dry_run) builds and returns the full request
    without ever sending it to eBay.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    with _create_listing_lock:
        try:
            result = create_listing(template_id=body.template_id, account_num=body.account_num,
                                     dry_run=body.dry_run, quiet=True)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"create failed: {e}")

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/jobs/market-price-refresh")
def market_price_refresh_endpoint(body: MarketPriceRefreshRequest, x_picking_token: str = Header(default="")):
    """
    Starts a market-price refresh job (docs/plans/listing-pricing-system.md)
    on a background thread and returns immediately with a job_id — the
    underlying Pokemon TCG API call is slow (per-card round trips, up to
    15 in flight at once), so this is not meant to be awaited like the
    other endpoints in this file. Poll GET /api/jobs/{job_id} for
    progress. No lock: unlike the eBay-write endpoints above, concurrent
    refresh jobs (e.g. for two different sets) don't contend with
    anything and are fine to run at once.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    if not body.set_name and not body.card_id:
        raise HTTPException(status_code=400, detail="set_name or card_id is required")
    if body.set_name and body.card_id:
        raise HTTPException(status_code=400, detail="pass set_name or card_id, not both")

    label = f"Market prices — {body.set_name}" if body.set_name else "Market prices — 1 card"
    job_id = start_job(
        "market_price_refresh", label, refresh_market_prices,
        set_name=body.set_name, card_id=body.card_id,
    )
    return {"job_id": job_id}


def _run_excel_import_and_cleanup(job_id, path, dry_run):
    """job_runner.start_job() target — deletes the temp upload once the
    import finishes (success or failure) so uploads don't pile up on disk."""
    try:
        return import_from_excel(path, dry_run=dry_run, verbose=False, job_id=job_id)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@app.post("/api/jobs/excel-import")
async def excel_import_endpoint(
    file: UploadFile = File(...),
    dry_run: bool = Form(False),
    x_picking_token: str = Header(default=""),
):
    """
    Uploads a filled-out spreadsheet (docs/plans/card_import_template.xlsx
    format) and runs it through the same Excel-to-staging importer as
    --excel-staging, on a background thread — resolution does live
    PokemonTCG API calls for anything not already in the local catalog,
    which can take a while for a big sheet, so this returns a job_id
    immediately instead of blocking the request. Poll GET /api/jobs/{job_id}
    for progress, same convention as /api/jobs/market-price-refresh.
    """
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    contents = await file.read()
    suffix = Path(file.filename or "upload.xlsx").suffix or ".xlsx"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(contents)
    tmp.close()

    label = f"Excel import — {file.filename or 'upload.xlsx'}"
    job_id = start_job("excel_import", label, _run_excel_import_and_cleanup,
                        path=tmp.name, dry_run=dry_run)
    return {"job_id": job_id}


@app.get("/api/jobs")
def list_jobs_endpoint(x_picking_token: str = Header(default="")):
    """Generic job list for the Jobs page — every job type started via
    start_job() shows up here automatically."""
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")
    return {"jobs": list_jobs()}


@app.get("/api/jobs/{job_id}")
def get_job_endpoint(job_id: str, x_picking_token: str = Header(default="")):
    if x_picking_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/api/picking/health")
def health():
    """No auth — lets the frontend distinguish 'endpoint down' (show stale
    banner) from 'pull failed' (show error) without spending a pull."""
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, host="0.0.0.0", port=int(os.getenv("PICKING_API_PORT", "8765")),
        ssl_certfile=os.getenv("PICKING_API_SSL_CERTFILE"),
        ssl_keyfile=os.getenv("PICKING_API_SSL_KEYFILE"),
    )
