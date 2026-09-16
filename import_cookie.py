"""Import cookie JSON files into Playwright account profile and register with BrowserPool."""
import asyncio
import json
import sys
from pathlib import Path
from patchright.async_api import async_playwright
from browser_pool import BrowserPool
import config


def parse_cookie_json(data: dict | list) -> tuple[list[dict], dict, list[str]]:
    """Normalizes cookie json from AccessHub Helper, Cookie-Editor, EditThisCookie, etc."""
    cookies_in = data.get("cookies", []) if isinstance(data, dict) else data
    if not isinstance(cookies_in, list):
        raise ValueError("Invalid format: cookies list not found in JSON data")

    pw_cookies = []
    cookie_str_parts = []
    for c in cookies_in:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
        c_out = {
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": str(c.get("domain") or ".dola.com"),
            "path": str(c.get("path") or "/"),
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        same_site = str(c.get("sameSite") or "")
        if same_site.lower() in ("no_restriction", "none"):
            c_out["sameSite"] = "None"
        elif same_site.lower() == "lax":
            c_out["sameSite"] = "Lax"
        elif same_site.lower() == "strict":
            c_out["sameSite"] = "Strict"

        if c.get("expirationDate"):
            try:
                c_out["expires"] = float(c["expirationDate"])
            except (ValueError, TypeError):
                pass

        pw_cookies.append(c_out)
        cookie_str_parts.append(f"{c['name']}={c['value']}")

    # If sessionid_ss is present but sessionid is missing, mirror it
    names = {c["name"] for c in pw_cookies}
    if "sessionid_ss" in names and "sessionid" not in names:
        ss = next(c for c in pw_cookies if c["name"] == "sessionid_ss")
        pw_cookies.append({**ss, "name": "sessionid"})
        cookie_str_parts.append(f"sessionid={ss['value']}")

    storage = data.get("storageByOrigin", {}) if isinstance(data, dict) else {}
    dola_storage = storage.get("https://www.dola.com", {}).get("localStorage", {})

    return pw_cookies, dola_storage, cookie_str_parts


async def import_account_from_data(account_name: str, data: dict | list) -> dict:
    """Creates accounts/<account_name> and injects cookies and local storage."""
    pw_cookies, dola_storage, cookie_str_parts = parse_cookie_json(data)
    if not pw_cookies:
        raise ValueError("No valid cookies found in provided data")

    profile_dir = Path("accounts") / account_name
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=True,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        await context.add_cookies(pw_cookies)
        await context.close()

    # Append to cookies.txt as secondary fallback
    try:
        with open("cookies.txt", "a", encoding="utf-8") as f:
            f.write("; ".join(cookie_str_parts) + "\n")
    except Exception:
        pass

    # Register in BrowserPool SQLite DB
    pool = BrowserPool()
    pool._ensure_meta(account_name)
    pool.set_email(account_name, f"imported_{account_name}")
    pool.set_login_status(account_name, True)

    return {
        "ok": True,
        "account": account_name,
        "cookie_count": len(pw_cookies),
        "has_storage": bool(dola_storage),
    }


async def main():
    file_path = sys.argv[1] if len(sys.argv) > 1 else "www-dola-com_profile_v2.json"
    account_name = sys.argv[2] if len(sys.argv) > 2 else "acc1"

    path = Path(file_path)
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    print(f"Reading cookie file: {path} ...")
    raw = json.loads(path.read_text(encoding="utf-8"))
    res = await import_account_from_data(account_name, raw)
    print(f"Successfully imported account '{res['account']}' with {res['cookie_count']} cookies!")


if __name__ == "__main__":
    asyncio.run(main())
