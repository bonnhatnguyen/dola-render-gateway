"""Automated Google OAuth login for account profile setup.

Usage: python add_account.py <account_name> "email----password----totp_secret"
"""
import asyncio
import base64
import hashlib
import hmac
import struct
import sys
import time
from pathlib import Path

from patchright.async_api import async_playwright

from browser import LAUNCH_ARGS
import config


def totp(secret: str, period: int = 30, digits: int = 6) -> str:
    """Standard TOTP (RFC 6238), Google Authenticator compatible."""
    secret = secret.replace(" ", "").upper()
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    counter = int(time.time() // period)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[19] & 15
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % (10 ** digits)
    return str(code).zfill(digits)


async def google_login(g, email: str, password: str, secret: str):
    """Executes Google OAuth login state machine until callback."""
    for step in range(25):
        await g.wait_for_timeout(2500)
        if "accounts.google.com" not in g.url:
            print("[google] Redirected out of Google domain (OAuth callback)", flush=True)
            return
        # 1) Account chooser page
        if "accountchooser" in g.url:
            acc = g.locator(f"text={email}").first
            if await acc.count() and await acc.is_visible():
                await acc.click(timeout=5000)
                print("[google] Account chooser page -> clicked account", flush=True)
                continue
        # 2) Email page
        identifier = g.locator("#identifierId, input[type='email'], input[name='identifier']").first
        if await identifier.count() and await identifier.is_visible():
            await identifier.fill(email)
            await g.wait_for_timeout(500)
            btn = g.locator("#identifierNext, button:has-text('Tiếp theo'), button:has-text('Next'), button:has-text('次へ')").first
            if await btn.count():
                await btn.click()
            else:
                await g.locator("#identifierNext").evaluate("e => e.click()")
            print("[google] Email page -> submit", flush=True)
            await g.wait_for_timeout(2000)
            continue
        # 3) Password page
        pwd = g.locator('input[name="Passwd"], input[type="password"], input[name="password"]').first
        if await pwd.count() and await pwd.is_visible():
            await g.wait_for_timeout(600)
            await pwd.fill(password)
            await g.wait_for_timeout(500)
            btn = g.locator("#passwordNext, button:has-text('Tiếp theo'), button:has-text('Next'), button:has-text('次へ')").first
            if await btn.count():
                await btn.click()
            else:
                await g.locator("#passwordNext").evaluate("e => e.click()")
            print("[google] Password page -> submit", flush=True)
            await g.wait_for_timeout(2000)
            continue
        # 4) Consent page
        clicked = False
        for sel in ["#submit_button",
                    "[role='button']:has-text('続行')", "button:has-text('続行')",
                    "[role='button']:has-text('继续')", "button:has-text('继续')",
                    "[role='button']:has-text('Tiếp tục')", "button:has-text('Tiếp tục')",
                    "[role='button']:has-text('Continue')", "button:has-text('Continue')",
                    "[role='button']:has-text('Cho phép')", "button:has-text('Cho phép')",
                    "[role='button']:has-text('Allow')", "button:has-text('Allow')"]:
            try:
                loc = g.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    print(f"[google] Consent page -> clicked {sel}", flush=True)
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            continue
        # 5) 2FA page
        totp_loc = g.locator('input[type="tel"], input#totpPin, input[name="totpPin"], input[name="Pin"]').first
        if await totp_loc.count() and await totp_loc.is_visible():
            if not secret:
                raise RuntimeError("Google requested 2FA, but no TOTP secret was provided")
            code = totp(secret)
            print(f"[google] 2FA page -> TOTP={code}", flush=True)
            await totp_loc.fill(code)
            await g.wait_for_timeout(500)
            btn = g.locator("#totpNext, button:has-text('Tiếp theo'), button:has-text('Next'), button:has-text('次へ')").first
            if await btn.count():
                await btn.click()
            else:
                await g.click("#totpNext")
            await g.wait_for_timeout(2000)
            continue
        # 6) Dola 18+ age confirmation popup
        if await g.locator("text=18").count():
            ok = await g.evaluate("""() => {
                const els = [...document.querySelectorAll('button, [role="button"], div, span')];
                const t = els.find(e => (e.textContent || '').trim() === 'OK' && e.childElementCount === 0);
                if (t) { t.click(); return true; }
                return false;
            }""")
            print(f"[dola] Age confirmation -> JS click OK = {ok}", flush=True)
            await g.wait_for_timeout(1500)
            continue
        txt = await g.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)")
        print(f"[google] step{step} unrecognized page url={g.url[:80]} text={txt[:200]}", flush=True)
    if "accounts.google.com" in g.url:
        await g.screenshot(path="dbg_google2.png")
        raise RuntimeError("Google login did not complete within 25 steps (saved dbg_google2.png)")


async def add_account_flow(account: str, email: str, password: str, secret: str) -> bool:
    """Full account addition flow; returns True on success."""
    profile_dir = Path("accounts") / account
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        kwargs = {"headless": False, "args": LAUNCH_ARGS,
                  "locale": "ja-JP", "timezone_id": "Asia/Tokyo"}
        if config.PROXY:
            kwargs["proxy"] = {"server": config.PROXY}
        context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000)
            await page.wait_for_timeout(3000)

            cookies = await context.cookies("https://www.dola.com")
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                print(f"[{account}] Active session exists, login not required", flush=True)
                return True

            # Dismiss cookie consent banner if present
            try:
                ok_btn = page.locator("button:has-text('OK'), text=OK").first
                if await ok_btn.count() and await ok_btn.is_visible():
                    await ok_btn.click(timeout=2000)
            except Exception:
                pass

            # Check if Google login button is already visible, if not click login entry
            google_btn = page.locator(
                "button:has-text('Googleで続ける'), [role='button']:has-text('Googleで続ける'), "
                "text=Googleで続ける, text=Continue with Google, button:has-text('Google')"
            ).first

            if not (await google_btn.count() and await google_btn.is_visible()):
                for sel in ["text=ログイン", "text=Log in", "text=Sign in", "text=Đăng nhập", "button:has-text('ログイン')", "button:has-text('Log in')"]:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=5000)
                        break
                await page.wait_for_timeout(1500)

            # Wait for Google login button to be visible
            await google_btn.wait_for(state="visible", timeout=15000)
            print(f"[{account}] Clicking Google login button...", flush=True)

            g = None
            try:
                async with context.expect_page(timeout=10000) as page_info:
                    await google_btn.click(force=True)
                g = await page_info.value
                print(f"[{account}] Detected popup page: {g.url}", flush=True)
            except Exception:
                pass

            if g is None:
                for _ in range(15):
                    await page.wait_for_timeout(1000)
                    for pg in context.pages:
                        if "accounts.google.com" in pg.url:
                            g = pg
                            break
                    if g is not None or "accounts.google.com" in page.url:
                        if g is None:
                            g = page
                        break

            if g is None:
                await page.screenshot(path="dbg_add_account.png")
                raise RuntimeError("Failed to open Google login page (saved dbg_add_account.png)")

            await g.wait_for_load_state("domcontentloaded")
            print(f"[{account}] Google login page ready: {g.url}", flush=True)
            await google_login(g, email, password, secret)

            # Wait for Dola sessionid after OAuth callback
            for _ in range(60):
                await page.wait_for_timeout(3000)
                cookies = await context.cookies("https://www.dola.com")
                if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                    print(f"[{account}] ✓ Login successful, sessionid saved to {profile_dir}", flush=True)
                    await page.wait_for_timeout(3000)
                    return True
            await page.screenshot(path="dbg_add_account.png")
            raise RuntimeError("sessionid not acquired within 3 minutes (saved dbg_add_account.png)")
        finally:
            await context.close()


async def main():
    account = sys.argv[1]
    email, password, secret = sys.argv[2].split("----")
    await add_account_flow(account, email, password, secret)
    print(f"[{account}] Account added successfully!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"✗ {e}")
        sys.exit(1)