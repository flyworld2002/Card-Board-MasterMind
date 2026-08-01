@echo off
rem run_picking_api.bat — keeps the CBM Picking API running on the desktop.
rem Registered via Task Scheduler (onlogon). Mirrors run_ebay_pull.bat style.
rem
rem Serves HTTPS via a Tailscale-issued cert (desktop-tu1m2fc.tail2c58d7.ts.net)
rem so the frontend — now hosted over HTTPS on Cloudflare Pages — can call it
rem without the browser blocking it as mixed content. Re-provision with
rem `tailscale cert desktop-tu1m2fc.tail2c58d7.ts.net` if it ever expires
rem (Tailscale auto-renews in the background while tailscaled is running, so
rem this should be rare).
cd /d C:\Users\Fei\Card-Board-Master\Card-Board-MasterMind
set PYTHONUTF8=1
python -m uvicorn picking_api:app --host 0.0.0.0 --port 8765 --ssl-certfile desktop-tu1m2fc.tail2c58d7.ts.net.crt --ssl-keyfile desktop-tu1m2fc.tail2c58d7.ts.net.key >> logs\picking_api.log 2>&1
