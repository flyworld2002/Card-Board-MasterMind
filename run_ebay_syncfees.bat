@echo off
cd /d C:\Users\Fei\Card-Board-Master\Card-Board-MasterMind
set PYTHONUTF8=1
python main.py --ebay-syncfees --since-days 14 >> logs\syncfees.log 2>&1