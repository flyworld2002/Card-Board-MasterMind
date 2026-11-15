@echo off
rem run_chrome_selftest.bat — checks that a scheduled (unattended) Claude Code
rem run can drive Chrome through the Claude in Chrome extension. The weekly
rem price refresh depends on this for its TCGplayer fallback.
rem Appends one RESULT line per run to logs\chrome_selftest.log.
cd /d C:\Users\Fei\Card-Board-Master\Card-Board-MasterMind
set PYTHONUTF8=1
echo [%date% %time%] selftest start >> logs\chrome_selftest.log
"C:\Users\Fei\.local\bin\claude.exe" -p --chrome --max-turns 25 --allowedTools "ToolSearch,mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__get_page_text,mcp__claude-in-chrome__tabs_close_mcp" < price_refresh\chrome_selftest_prompt.txt >> logs\chrome_selftest.log 2>&1
echo [%date% %time%] selftest end (exit %errorlevel%) >> logs\chrome_selftest.log
