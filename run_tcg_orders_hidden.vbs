' Runs run_tcg_orders.bat with no console window, so the 15-minute
' CBMPriceRefresh scheduled task does not flash a window on the desktop.
' Waits for the batch file to finish, which keeps Task Scheduler from
' starting a second copy while a refresh is running.
CreateObject("WScript.Shell").Run """C:\Users\Fei\Card-Board-Master\Card-Board-MasterMind\run_tcg_orders.bat""", 0, True
