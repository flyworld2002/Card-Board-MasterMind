@echo off
rem Weekly price refresh tick. Task Scheduler runs this every 15 minutes;
rem it only does work when a run is due (see importer\price_refresh_job.py).
cd /d C:\Users\Fei\Card-Board-Master\Card-Board-MasterMind
set PYTHONUTF8=1
python -u main.py --price-refresh-tick >> logs\price_refresh.log 2>&1
