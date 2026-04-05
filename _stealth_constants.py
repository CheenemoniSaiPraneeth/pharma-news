# _stealth_constants.py

import random
import asyncio

# ── USER AGENT ─────────────────────────────────────────
STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ── HEADERS ────────────────────────────────────────────
EXTRA_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
}

REQUESTS_HEADERS = {
    "User-Agent": STEALTH_UA,
    "Accept-Language": "en-US,en;q=0.9",
}

# ── PLAYWRIGHT ARGS ────────────────────────────────────
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
]

# ── STEALTH JS ─────────────────────────────────────────
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
"""

# ── APPLY STEALTH (CONTEXT) ────────────────────────────
async def apply_stealth_context(context):
    await context.add_init_script(STEALTH_JS)
    await context.set_extra_http_headers(EXTRA_HEADERS)

# ── APPLY STEALTH (PAGE) ───────────────────────────────
async def apply_stealth_page(page):
    await page.add_init_script(STEALTH_JS)

# ── HUMAN DELAY ────────────────────────────────────────
async def random_human_delay(a=0.5, b=2.0):
    await asyncio.sleep(random.uniform(a, b))

# ── MOUSE MOVE (OPTIONAL) ──────────────────────────────
async def human_mouse_move(page):
    try:
        await page.mouse.move(random.randint(0, 500), random.randint(0, 500))
    except:
        pass