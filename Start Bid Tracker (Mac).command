#!/bin/bash
cd "$(dirname "$0")"
echo "Ohio Vehicle Bid Tracker - checking every 15 minutes. Leave this window open."
python3 scraper.py --watch 15
