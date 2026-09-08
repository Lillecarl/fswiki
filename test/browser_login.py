#!/usr/bin/env python3
"""Log in through dex, in a browser, and read the wiki with what comes back.

Runs on the *host*, against ports passt forwards out of the guest, which is
also where a person's browser is in real life. `browser.py` starts it.

It has to be the host. io_uring does not work under User-Mode Linux -- the
guest prints "BUG: Bad page map ... file:[io_uring]" and then stops answering
altogether -- and every browser reaches for it, chromium and firefox alike. So
a browser cannot run in the guest at all, and this is not a workaround so much
as the arrangement that was always more honest.

What this covers that `integration.py` does not is the half of OIDC a password
grant skips: dex's authorization endpoint, its login page, the consent step, and
the redirect carrying a code back. A real browser, a real form, real
credentials.

What it does not cover is fswiki starting the flow or handling the callback,
because fswiki does neither yet -- see docs/oidc.md. So this script is the
relying party: it exchanges the code itself and puts the token in the
`fswiki_session` cookie the server already reads. When there is a login, the
exchange moves into the server and everything above it here stays as it is.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import expect, sync_playwright

# The wiki and the provider, as this browser reaches them. Arguments rather than
# constants: the browser runs on the host and the services are in a guest, so
# what it dials is a forwarded address the runner hands down.
WIKI, DEX = sys.argv[1], sys.argv[2]
REDIRECT = f"{WIKI}/callback"

CLIENT_ID = "fswiki"
CLIENT_SECRET = "fswiki-test-secret"
USERNAME = "bob@test.invalid"
PASSWORD = "hunter2"

# A page bob may read and a visitor may not, so that one navigation can tell
# the two apart. Both come from the suite's fixtures.
PAGE = "/engineering/onboarding"


def exchange(code: str) -> str:
    """The code for a token. This is the part a server would do."""
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    with urllib.request.urlopen(f"{DEX}/token", data=body, timeout=30) as r:
        payload = json.load(r)
    token = payload.get("id_token")
    if not token:
        raise SystemExit(f"dex returned no id_token: {payload}")
    return token


def main() -> int:
    auth = f"{DEX}/auth?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        # `groups` is what carries the database role. Asking for it here is the
        # browser-flow half of what the password grant asks for with -d scope.
        "scope": "openid groups email profile",
        "state": "fswiki-browser-test",
    })

    with sync_playwright() as p:
        browser = p.firefox.launch()
        context = browser.new_context()
        page = context.new_page()

        # --- dex's login page, in a browser --------------------------------
        page.goto(auth, wait_until="domcontentloaded")
        # One connector, so dex goes straight to the password form rather than
        # offering a choice.
        page.wait_for_selector("#login", timeout=30_000)
        print("[browser] dex served its login page")

        page.fill("#login", USERNAME)
        page.fill("#password", PASSWORD)
        page.click("#submit-login")

        # Consent, if this client asks for it. Handled rather than switched off,
        # because a page that only sometimes appears is the kind of thing that
        # makes a browser test flaky six months later.
        page.wait_for_load_state("domcontentloaded")
        grant = page.locator("button:has-text('Grant Access')")
        if grant.count():
            grant.first.click()
            page.wait_for_load_state("domcontentloaded")
            print("[browser] granted access")

        # --- the redirect, carrying a code ---------------------------------
        page.wait_for_url(f"{REDIRECT}**", timeout=30_000)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
        if "error" in query:
            raise SystemExit(f"dex refused: {query}")
        code = query.get("code", [""])[0]
        assert code, f"no code in {page.url}"
        assert query.get("state") == ["fswiki-browser-test"], query
        print("[browser] dex redirected with a code")

        token = exchange(code)
        print("[browser] and the code became a token")

        # --- what a visitor sees, for comparison ---------------------------
        #
        # Taken first and in this same browser, so the difference below cannot
        # be anything but the cookie.
        page.goto(WIKI + PAGE, wait_until="domcontentloaded")
        anonymous = page.content()

        # --- and what bob sees ---------------------------------------------
        context.add_cookies([{
            "name": "fswiki_session",
            "value": token,
            "domain": urllib.parse.urlparse(WIKI).hostname,
            "path": "/",
        }])
        page.goto(WIKI + PAGE, wait_until="domcontentloaded")
        expect(page.locator("body")).to_contain_text("onboarding", ignore_case=True)
        signed_in = page.content()

        assert signed_in != anonymous, (
            "the page is the same signed in and out; the cookie did nothing")
        print("[browser] the wiki served bob the page it would not serve a visitor")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
