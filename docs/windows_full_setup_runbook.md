# Windows desktop — full setup runbook

Ordered start-to-finish. Run from an elevated (Administrator) Command Prompt
unless noted. Replace `C:\actual\path\to\...` with your real project path.

---

## 1. Get the code

```
git clone https://github.com/flyworld2002/Card-Board-MasterMind.git
cd Card-Board-MasterMind
```
(or `git pull origin main` if the folder already exists)

---

## 2. Python dependencies

```
pip install fastapi uvicorn requests python-dotenv psycopg2-binary
```
If the project has a `requirements.txt`, prefer:
```
pip install -r requirements.txt
```
and only add `fastapi`/`uvicorn` on top if they're not already listed (they
were added this session for the Picking API and may not be in an older
requirements file yet).

---

## 3. `.env` file (project root, gitignored — must be created/edited by hand)

Keys confirmed needed by this project:
```
EBAY_ACCOUNT_1_REFRESH_TOKEN=<from the token flow below>
EBAY_ACCOUNT_1_RUNAME=<your keyset's RuName, needed to mint/re-mint>
PICKING_API_TOKEN=<any long random string — must match picking.js exactly>
PICKING_API_PORT=8765   # optional, defaults to 8765 if omitted
```
Plus whatever Trading API / app credential keys and database connection
string your existing `.env` already has (app ID, cert ID, dev ID, DB URL) —
**check `importer/ebay_auth.py` and `db/connection.py` for the exact key
names**, since those weren't part of this session's changes and I don't
want to guess at names I haven't verified. Easiest path: copy the working
`.env` from the Mac, then update `EBAY_ACCOUNT_1_REFRESH_TOKEN` and add
`PICKING_API_TOKEN` — everything else should already be correct.

Generate a random token for `PICKING_API_TOKEN`:
```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## 3.5. Getting the eBay refresh token — first time vs. rotation

The refresh token (`EBAY_ACCOUNT_1_REFRESH_TOKEN`) is what lets the app call
the Finances/Fulfillment APIs without you re-approving every 2 hours. It's
long-lived (~18 months) but eventually needs replacing — either because it
expired, or because you're rotating it after an exposure (like this
session's chat-paste incident). Two different situations:

### A. No refresh token exists yet (brand new setup)
This requires the RuName to be registered on developer.ebay.com first (blank
accept/decline URLs, so consent lands on eBay's own hosted result page —
already set up for this project, shouldn't need redoing). Then run:
```
python3 rotate_ebay_token.py --step1 --account 1
```
Open the printed URL, log in as the eBay seller account, approve, copy the
`code=...` from the resulting page's address bar, then immediately (codes
are short-lived and single-use):
```
python3 rotate_ebay_token.py --step2 --account 1 --code "PASTE_CODE_HERE"
```
This prints a brand-new refresh token to your terminal only. Put it in
`.env` as shown above.

### B. Rotating an existing token (exposure, or approaching 18-month expiry)
Same two commands as above (A) — minting a new token via the consent flow
works whether or not one already existed. The only extra step is revoking
the old one so it stops working immediately, rather than just letting a
second valid token exist alongside it:

1. Log into **My eBay as the seller account** → Account → **Sign in and
   security** → scroll to the very bottom → **Third-party app access** →
   **View**.
2. Find your app in the list (may show by project name or by App ID
   string), check it, click **Revoke**.
3. Run the same `--step1` / `--step2` commands as scenario A to mint the
   replacement.
4. Update `.env` on **both** machines (Mac + Windows desktop — `.env` is
   gitignored, doesn't travel via `git pull`).
5. Restart anything holding the old token in memory: `picking_api.py`
   (`schtasks /run /tn "CBMPickingAPI"` after stopping the running one), and
   let the next scheduled `EbayPullOrders`/`EbaySyncFees` run pick up the
   new token naturally.
6. Verify it actually works:
   ```
   python3 main.py --ebay-finances-test --fin-order <any real recent order id>
   ```
   A real transaction list back (not an auth error) confirms it's live.

**Reminder for whoever's doing this ~18 months from now**: there's no
automated expiry warning wired into this project — the refresh token will
just start failing auth once it lapses. Worth a calendar reminder rather
than waiting for it to break.

---

## 4. Firewall rule (so the Mac can reach the Picking API over the LAN)

```
netsh advfirewall firewall add rule name="CBM Picking API" dir=in action=allow protocol=TCP localport=8765
```

---

## 5. Find the desktop's LAN IP (needed for `picking.js` on the Mac)

```
ipconfig
```
Look for `IPv4 Address` under the active adapter. Reserve this IP in your
router's DHCP settings so it doesn't change later. Put it into `picking.js`'s
`PICKING_API_URL` (on the Mac, then commit/push).

---

## 6. Verify each script manually before scheduling anything

```
python main.py --ebay-pullorders --quiet --dry-run
python main.py --ebay-syncfees --since-days 14 --dry-run
python main.py --ebay-pullpicking --dry-run
```
All three should run without errors. Fix anything that fails here before
moving on — scheduling a broken script just automates the failure.

Test the Picking API directly (foreground, so you see errors live):
```
python -m uvicorn picking_api:app --host 0.0.0.0 --port 8765
```
From the Mac's browser: `http://<desktop-ip>:8765/api/picking/health`
should return `{"ok":true}`. Ctrl+C to stop once confirmed.

---

## 7. Edit the `.bat` wrapper paths

Open `run_ebay_pull.bat`, `run_ebay_syncfees.bat`, `run_picking_api.bat` and
confirm each `cd /d C:\...` line points at your actual project folder.

---

## 8. Register the scheduled tasks

```
schtasks /create /tn "EbayPullOrders" /tr "C:\actual\path\run_ebay_pull.bat" /sc minute /mo 15 /ru "%USERNAME%"
schtasks /create /tn "EbaySyncFees" /tr "C:\actual\path\run_ebay_syncfees.bat" /sc daily /st 04:00 /ru "%USERNAME%"
schtasks /create /tn "CBMPickingAPI" /tr "C:\actual\path\run_picking_api.bat" /sc onlogon /ru "%USERNAME%"
```

---

## 9. Fire each task once to confirm registration works

```
schtasks /run /tn "EbayPullOrders"
schtasks /run /tn "EbaySyncFees"
schtasks /run /tn "CBMPickingAPI"
```
Check the logs:
```
type logs\pull.log
type logs\syncfees.log
type logs\picking_api.log
```
And re-check the Picking API health endpoint from the Mac once more, now
that it's running via the scheduled task rather than manually.

---

## 10. Recommended (not yet done, defensive measure)

Prevent `EbayPullOrders` from ever double-running if a pull takes longer
than 15 minutes — not settable via plain `schtasks /create`:
Task Scheduler GUI → find `EbayPullOrders` → Properties → Settings tab →
"If the task is already running, then the following rule applies" →
**Do not start a new instance**.

---

## Everyday commands, once everything above is done

```
schtasks /query /tn "EbayPullOrders"          # check status / next run
schtasks /query /tn "EbaySyncFees" /fo LIST /v  # full detail
tasklist | findstr python                      # confirm picking API is alive
schtasks /run /tn "CBMPickingAPI"              # manually trigger any task
schtasks /change /tn "EbayPullOrders" /disable # pause without deleting
schtasks /change /tn "EbayPullOrders" /enable  # resume
```
