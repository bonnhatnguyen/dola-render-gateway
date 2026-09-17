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
        totp_loc = g.locator('input[type="tel"], input#totpPin, input[name="totpPin"], input[name="Pin"], input[autocomplete="one-time-code"]').first
        if await totp_loc.count() and await totp_loc.is_visible():
            if not secret:
                raise RuntimeError("Google yêu cầu xác thực 2FA nhưng không có mã bí mật TOTP Secret Key")
            code = totp(secret)
            print(f"[google] 2FA page -> TOTP={code}", flush=True)
            await totp_loc.fill(code)
            await g.wait_for_timeout(500)
            btn = g.locator("#totpNext, button:has-text('Tiếp theo'), button:has-text('Next'), button:has-text('次へ')").first
            if await btn.count() and await btn.is_visible():
                await btn.click()
            else:
                try:
                    await g.click("#totpNext")
                except Exception:
                    await totp_loc.press("Enter")
            await g.wait_for_timeout(2000)
            continue

        # 5b) Challenge fallback: if on challenge page but totp field not visible, click Try another way -> Authenticator
        if "challenge" in g.url:
            for try_another_sel in [
                "button:has-text('Try another way')", "button:has-text('Thử cách khác')", "button:has-text('別の方法を試す')",
                "[role='button']:has-text('Try another way')", "[role='button']:has-text('Thử cách khác')", "[role='button']:has-text('別の方法を試す')",
                "text=Try another way", "text=Thử cách khác", "text=別の方法を試す",
            ]:
                try:
                    alt_btn = g.locator(try_another_sel).first
                    if await alt_btn.count() and await alt_btn.is_visible():
                        await alt_btn.click(timeout=3000)
                        await g.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

            for auth_sel in [
                "div:has-text('Google Authenticator')", "li:has-text('Google Authenticator')",
                "div:has-text('Authenticator')", "li:has-text('Authenticator')",
                "[data-challengetype='6']", "text=Google Authenticator",
            ]:
                try:
                    opt_btn = g.locator(auth_sel).first
                    if await opt_btn.count() and await opt_btn.is_visible():
                        await opt_btn.click(timeout=3000)
                        await g.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

        # 6) Dola 18+ age confirmation popup
        try:
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
        except Exception:
            pass

        txt = await g.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)")
        print(f"[google] step{step} unrecognized page url={g.url[:80]} text={txt[:200]}", flush=True)

    if "accounts.google.com" in g.url:
        await g.screenshot(path="dbg_google2.png")
        raise RuntimeError("Google login did not complete within 30 steps (saved dbg_google2.png)")


COOKIE_BANNER_SELECTORS = [
    "button:has-text('OK')",
    "[role='button']:has-text('OK')",
    "text=OK",
    "button:has-text('Accept')",
    "button:has-text('同意')",
]

GOOGLE_BUTTON_SELECTORS = [
    "button:has-text('Googleで続ける')",
    "[role='button']:has-text('Googleで続ける')",
    "text=Googleで続ける",
    "button:has-text('Continue with Google')",
    "[role='button']:has-text('Continue with Google')",
    "text=Continue with Google",
    "button:has-text('Google')",
    "[role='button']:has-text('Google')",
]

LOGIN_ENTRY_SELECTORS = [
    "button:has-text('ログイン')",
    "[role='button']:has-text('ログイン')",
    "text=ログイン",
    "button:has-text('Log in')",
    "[role='button']:has-text('Log in')",
    "text=Log in",
    "button:has-text('Sign in')",
    "[role='button']:has-text('Sign in')",
    "text=Sign in",
    "button:has-text('Đăng nhập')",
    "[role='button']:has-text('Đăng nhập')",
    "text=Đăng nhập",
]


async def _find_first_visible(page, selectors: list[str]):
    """Returns the first visible locator among candidate selectors without throwing syntax errors."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


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
            if any(c["name"] in ("sessionid", "sessionid_ss") and c.get("value") for c in cookies):
                print(f"[{account}] Active session exists, login not required", flush=True)
                return True

            # Dismiss cookie consent banner if present
            try:
                ok_btn = await _find_first_visible(page, COOKIE_BANNER_SELECTORS)
                if ok_btn:
                    await ok_btn.click(timeout=2000)
            except Exception:
                pass

            # Check if Google login button is already visible, if not click login entry
            google_btn = await _find_first_visible(page, GOOGLE_BUTTON_SELECTORS)
            if not google_btn:
                entry_btn = await _find_first_visible(page, LOGIN_ENTRY_SELECTORS)
                if entry_btn:
                    try:
                        await entry_btn.click(timeout=5000)
                        await page.wait_for_timeout(1500)
                    except Exception:
                        pass

            # Wait for Google login button to be visible (up to 15s)
            google_btn = None
            for _ in range(15):
                google_btn = await _find_first_visible(page, GOOGLE_BUTTON_SELECTORS)
                if google_btn:
                    break
                await page.wait_for_timeout(1000)

            if not google_btn:
                await page.screenshot(path="dbg_add_account.png")
                raise RuntimeError("Không tìm thấy nút đăng nhập Google trên trang Dola (đã chụp dbg_add_account.png)")

            print(f"[{account}] Clicking Google login button...", flush=True)

            g = None
            try:
                async with context.expect_page(timeout=8000) as page_info:
                    try:
                        await google_btn.click(timeout=5000)
                    except Exception:
                        await google_btn.evaluate("e => e.click()")
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
                # Try clicking via JS evaluate in case normal click was intercepted
                try:
                    await google_btn.evaluate("e => e.click()")
                    for _ in range(10):
                        await page.wait_for_timeout(1000)
                        for pg in context.pages:
                            if "accounts.google.com" in pg.url:
                                g = pg
                                break
                        if g is not None or "accounts.google.com" in page.url:
                            if g is None:
                                g = page
                            break
                except Exception:
                    pass

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
                if any(c["name"] in ("sessionid", "sessionid_ss") and c.get("value") for c in cookies):
                    print(f"[{account}] ✓ Login successful, sessionid saved to {profile_dir}", flush=True)
                    c_parts = [f"{c['name']}={c['value']}" for c in cookies if c.get("value")]
                    try:
                        with open("cookies.txt", "a", encoding="utf-8") as f:
                            f.write("; ".join(c_parts) + "\n")
                    except Exception:
                        pass
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