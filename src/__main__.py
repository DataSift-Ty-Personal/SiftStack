"""Apify Actor entry point — allows `python -m src` invocation."""

import os
import sys

# Add src/ to path so all local imports (config, scraper, etc.) resolve
sys.path.insert(0, os.path.dirname(__file__))

import asyncio
from main import actor_main

asyncio.run(actor_main())
