"""Refresh the Smarter Contact login the SMS agent on Fly signs in with.

Reads the Firebase session the web app keeps in IndexedDB (saved profile), mints a
fresh ID token through Google's securetoken endpoint with plain HTTP, and makes one
read-only GraphQL call with it. Prints booleans and lengths only, never a secret.
With --save, writes the two values to .env (gitignored). Then push them to Fly:

    grep -E "^SMARTERCONTACT_(FIREBASE_API_KEY|REFRESH_TOKEN)=" .env | fly secrets import -a siftstack

Needed after a password change or "sign out of all devices", which kills the token.
"""
import asyncio
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.async_api import async_playwright  # noqa: E402

import smarter_contact_launch as L  # noqa: E402

READ_IDB = """() => new Promise((resolve) => {
  const req = indexedDB.open('firebaseLocalStorageDb');
  req.onerror = () => resolve(null);
  req.onsuccess = () => {
    const db = req.result;
    const tx = db.transaction('firebaseLocalStorage', 'readonly');
    const all = tx.objectStore('firebaseLocalStorage').getAll();
    all.onsuccess = () => resolve(all.result);
    all.onerror = () => resolve(null);
  };
})"""


async def main(save: bool):
    async with async_playwright() as pw:
        ctx, s = await L._session(pw, "/messenger/inbox/all")
        rows = await s.page.evaluate(READ_IDB)
        await ctx.close()
    user = next((r for r in rows or [] if str(r.get("fbase_key", "")).startswith("firebase:authUser:")), None)
    if not user:
        print("no Firebase user record found in IndexedDB")
        return 1
    api_key = user["fbase_key"].split(":")[2]
    refresh = ((user.get("value") or {}).get("stsTokenManager") or {}).get("refreshToken", "")
    print("api key found:", bool(api_key), "| refresh token length:", len(refresh))

    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh}).encode()
    req = urllib.request.Request(f"https://securetoken.googleapis.com/v1/token?key={api_key}", data=body,
                                 headers={"content-type": "application/x-www-form-urlencoded"})
    tok = json.loads(urllib.request.urlopen(req, timeout=20).read())
    id_token = tok.get("id_token", "")
    print("minted id token:", bool(id_token), "| expires in:", tok.get("expires_in"), "s",
          "| refresh token rotated:", tok.get("refresh_token") != refresh)

    gql = urllib.request.Request("https://api.smartercontact.com/gql",
                                 data=json.dumps({"query": "query { getContactsCounters { activeContacts } }"}).encode(),
                                 headers={"content-type": "application/json", "authorization": f"Bearer {id_token}"})
    res = json.loads(urllib.request.urlopen(gql, timeout=20).read())
    print("server-side GraphQL read:", json.dumps(res.get("data") or res.get("errors"))[:160])

    if save:
        env = Path(__file__).resolve().parent.parent / ".env"
        lines = [l for l in env.read_text(encoding="utf-8").splitlines()
                 if not l.startswith(("SMARTERCONTACT_FIREBASE_API_KEY=", "SMARTERCONTACT_REFRESH_TOKEN="))]
        lines += [f"SMARTERCONTACT_FIREBASE_API_KEY={api_key}",
                  f"SMARTERCONTACT_REFRESH_TOKEN={tok.get('refresh_token') or refresh}"]
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("saved both values to .env")
    return 0


sys.exit(asyncio.run(main("--save" in sys.argv)))
