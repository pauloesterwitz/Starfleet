#!/usr/bin/env python3
"""Stands in for the Telegram send script: appends the message it is given to a file instead of sending it.
Exits 1, a failed send, while the file named by E2E_TELEGRAM_FAIL exists."""
import json
import os
import sys
import time

if os.path.exists(os.environ["E2E_TELEGRAM_FAIL"]):
    sys.exit(1)
with open(os.environ["E2E_TELEGRAM_LOG"], "a") as f:
    f.write(json.dumps({"at": time.time(), "text": sys.argv[1]}) + "\n")
