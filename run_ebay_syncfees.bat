@echo off
cd /d "C:\path\to\card_inventory"
python main.py --ebay-syncfees --since-days 14 >> logs\syncfees.log 2>&1
