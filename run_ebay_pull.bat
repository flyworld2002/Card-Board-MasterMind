@echo off
cd /d C:\Users\Fei\Card-Board-MasterMind
python main.py --ebay-pullorders --quiet >> logs\pull.log 2>&1
