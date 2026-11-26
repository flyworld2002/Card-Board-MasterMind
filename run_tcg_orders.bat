@echo off
rem TCGplayer order tracker: reads new order pages from the Dropbox inbox.
rem Task Scheduler runs this every 15 minutes (see importer\tcg_orders.py).
cd /d C:\Users\Fei\Card-Board-Master\Card-Board-MasterMind
set PYTHONUTF8=1
python -u main.py --tcg-orders-process >> logs\tcg_orders.log 2>&1
