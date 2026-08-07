@echo off
cd /d C:\Users\Fei\Card-Board-Master\Card-Board-MasterMind
set PYTHONUTF8=1
python main.py --ebay-pullorders --quiet >> logs\pull.log 2>&1
