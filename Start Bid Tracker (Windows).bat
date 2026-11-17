@echo off
cd /d "%~dp0"
echo Ohio Vehicle Bid Tracker - checking every 15 minutes. Leave this window open.
where py >nul 2>nul && (py -3 scraper.py --watch 15) || (python scraper.py --watch 15)
pause
