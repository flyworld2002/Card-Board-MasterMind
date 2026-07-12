@echo off
rem run_picking_api.bat — keeps the CBM Picking API running on the desktop.
rem Registered via Task Scheduler (onlogon). Mirrors run_ebay_pull.bat style.
cd /d C:\Users\Fei\Card-Board-MasterMind
set PYTHONUTF8=1
python -m uvicorn picking_api:app --host 0.0.0.0 --port 8765 >> logs\picking_api.log 2>&1
