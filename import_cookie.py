"""Import cookie JSON files into Playwright account profile and register with BrowserPool."""
import asyncio
import json
import sys
from pathlib import Path
from patchright.async_api import async_playwright
from browser_pool import BrowserPool
import config


def parse_cookie_json(data: dict | list | str) -> tuple[list[dict], dict, list[str]]:
    """Normalizes cookie data from JSON array, JSON object, Netscape format, or key-value string."""
    dola_storage = {}
    cookies_in = []

    if isinstance(data, str):
        s = data.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                parsed = json.loads(s)
                return parse_cookie_json(parsed)
            except Exception:
                pass

        # Try Netscape format
        lines = [line.strip() for line in s.splitlines() if line.strip()]
        for line in lines:
            if line.startswith("# Netscape") or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            is_http = False
            if line.startswith("#HttpOnly_"):
                is_http = True
                line = line[len("#HttpOnly_"):]
            parts = line.split("\t")
            if len(parts) < 7:
                parts = line.split()
            if len(parts) >= 7:
                cookies_in.append({
                    "name": parts[5],
                    "value": parts[6],
                    "domain": parts[0],
                    "path": parts[2],
                    "secure": parts[3].upper() == "TRUE",
                    "httpOnly": is_http,
                    "expirationDate": parts[4],
                })

        # Try key-value header format: a=b; c=d
        if not cookies_in:
            for chunk in s.replace("\n", ";").split(";"):
                chunk = chunk.strip()
                if "=" in chunk:
                    k, v = chunk.split("=", 1)
                    if k.strip():
                        cookies_in.append({
                            "name": k.strip(),
                            "value": v.strip(),
                            "domain": ".dola.com",
                            "path": "/",
                            "secure": True,
                        })
    elif isinstance(data, dict):
        if "cookies" in data and isinstance(data["cookies"], list):
            cookies_in = data["cookies"]
        else:
            cookies_in = [{"name": k, "value": str(v)} for k, v in data.items() if isinstance(v, (str, int, float))]
        storage = data.get("storageByOrigin", {}) if isinstance(data, dict) else {}
        dola_storage = storage.get("https://www.dola.com", {}).get("localStorage", {})
    elif isinstance(data, list):
        cookies_in = data

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

    return pw_cookies, dola_storage, cookie_str_parts


async def import_account_from_data(account_name: str, data: dict | list | str) -> dict:
    """Creates accounts/<account_name> and injects cookies and local storage."""
    pw_cookies, dola_storage, cookie_str_parts = parse_cookie_json(data)
    if not pw_cookies:
        raise ValueError("No valid cookies found in provided data")

    profile_dir = Path("accounts") / account_name
    profile_dir.mkdir(parents=True, exist_ok=True)

    from browser import LAUNCH_ARGS

    async with async_playwright() as p:
        kwargs = {
            "headless": True,
            "args": LAUNCH_ARGS,
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
        }
        if config.PROXY:
            kwargs["proxy"] = {"server": config.PROXY}
        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            **kwargs,
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
