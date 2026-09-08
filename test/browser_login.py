#!/usr/bin/env python3
"""Log in through dex as each of several people, and photograph what they see.

Runs on the *host*, against ports passt forwards out of the guest, which is also
where a person's browser is in real life. `browser.py` starts it.

It has to be the host. io_uring does not work under User-Mode Linux -- the guest
prints "BUG: Bad page map ... file:[io_uring]" and then stops answering at all --
and every browser reaches for it, chromium and firefox alike. So a browser cannot
run in the guest, and this is less a workaround than the arrangement that was
always more honest.

Two jobs, and the second is why the first is worth doing in a browser at all.

**It logs in.** dex's authorization endpoint, its login form, consent, and the
redirect carrying a code back. A password grant skips every one of those.

**It photographs the result.** The same six pages, as each of four people, into
$out. An ACL is a prediction about what somebody will be able to see, and a
screenshot is the only artefact that answers "does it *look* right" -- a child
missing from a tree, a wikilink that rendered as plain text because its target is
forbidden. An assertion would say a number changed. The pictures say what.

fswiki neither starts the flow nor handles the callback, because there is no
login yet -- see docs/oidc.md. So this script is the relying party: it exchanges
the code itself and puts the token in the `fswiki_session` cookie the server
already reads. When there is a login, the exchange moves into the server and
everything else here stays as it is.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

WIKI, DEX, OUT, PNGQUANT = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
IDENTITIES = json.loads(sys.argv[5])

REDIRECT = f"{WIKI}/callback"
CLIENT_ID = "fswiki"
CLIENT_SECRET = "fswiki-test-secret"
PASSWORD = "hunter2"

# What to photograph, and why each one is in the list.
#
# Every path is somewhere the fixtures make one person's view differ from
# another's. A page nobody is refused would come out identically four times and
# say nothing.
PAGES = [
    ("01-root", "/", "the tree, and what is pruned out of it"),
    ("02-engineering", "/engineering",
     "a folder bob edits, carol is denied across, and erin cannot see"),
    ("03-onboarding", "/engineering/onboarding",
     "carol's explicit allow, outranking the inherited deny above it"),
    ("04-secret-plans", "/engineering/secret-plans",
     "the same folder with no explicit allow to rescue it"),
    ("05-private-memo", "/engineering/private/memo",
     "where bob's inheritance stops, because `private` blocks it"),
    ("06-guide", "/public/guide",
     "public prose, whose wikilinks point at pages not everyone may read"),
]

# Wide enough not to catch a page mid-reflow. full_page extends the height where
# a page is longer than this.
VIEWPORT = {"width": 1100, "height": 800}


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


def log_in(context, user: str) -> str:
    """Drive dex's browser flow as `user`, and come back with a token."""
    page = context.new_page()
    page.goto(f"{DEX}/auth?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        # `groups` is what carries the database role. It is a scope: a token
        # does not have it unless it was asked for.
        "scope": "openid groups email profile",
        "state": f"fswiki-{user}",
    }), wait_until="domcontentloaded")

    # One connector, so dex goes straight to the password form rather than
    # offering a choice of them.
    page.wait_for_selector("#login", timeout=30_000)
    page.fill("#login", f"{user}@test.invalid")
    page.fill("#password", PASSWORD)
    page.click("#submit-login")

    # Consent, if this client asks for it. Handled rather than switched off: a
    # page that only sometimes appears is what makes a browser test flaky six
    # months later.
    page.wait_for_load_state("domcontentloaded")
    grant = page.locator("button:has-text('Grant Access')")
    if grant.count():
        grant.first.click()
        page.wait_for_load_state("domcontentloaded")

    page.wait_for_url(f"{REDIRECT}**", timeout=30_000)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
    if "error" in query:
        raise SystemExit(f"dex refused {user}: {query}")
    code = query.get("code", [""])[0]
    assert code, f"no code in {page.url}"
    assert query.get("state") == [f"fswiki-{user}"], query
    page.close()
    return exchange(code)


def photograph(context, out: pathlib.Path) -> int:
    """Every page in PAGES, as whoever this context is. Returns bytes written."""
    out.mkdir(parents=True, exist_ok=True)
    page = context.new_page()
    total = 0
    for name, path, _ in PAGES:
        page.goto(WIKI + path, wait_until="networkidle")
        shot = out / f"{name}.png"
        page.screenshot(path=str(shot), full_page=True)
        # Lossy palette quantisation. A wiki page is flat colour and a couple of
        # typefaces, which is what pngquant is best at: about a fifth of the
        # size, with nothing a reader would notice. --skip-if-larger because on
        # the smallest images it sometimes is.
        subprocess.run(
            [PNGQUANT, "--force", "--skip-if-larger", "--quality=60-90",
             "--output", str(shot), str(shot)],
            check=False,
        )
        total += shot.stat().st_size
    page.close()
    return total


def main() -> int:
    out = pathlib.Path(OUT) / "screens"
    seen = []

    with sync_playwright() as p:
        browser = p.firefox.launch()

        # A visitor first, in a context of its own, so what follows is a
        # comparison rather than a claim.
        anon = browser.new_context(viewport=VIEWPORT)
        photograph(anon, out / "anonymous")
        probe = anon.new_page()
        probe.goto(WIKI + "/engineering/onboarding", wait_until="networkidle")
        anonymous_body = probe.content()
        anon.close()
        seen.append("anonymous")
        print("[browser] photographed a visitor with no token")

        for user in IDENTITIES:
            # A context each. Sharing one would carry dex's own session cookie
            # from one login into the next, and the second person would never be
            # shown a form -- which is a real thing to want to test, and not
            # this.
            context = browser.new_context(viewport=VIEWPORT)
            token = log_in(context, user)
            context.add_cookies([{
                "name": "fswiki_session",
                "value": token,
                "domain": urllib.parse.urlparse(WIKI).hostname,
                "path": "/",
            }])
            written = photograph(context, out / user)
            print(f"[browser] {user}: logged in, {len(PAGES)} pages, "
                  f"{written // 1024} KiB")

            if user == "bob":
                # The one assertion worth keeping in code. The rest of this is
                # for a person to look at, but "the cookie did something" is a
                # thing a machine should notice going wrong.
                page = context.new_page()
                page.goto(WIKI + "/engineering/onboarding",
                          wait_until="networkidle")
                assert page.content() != anonymous_body, (
                    "bob's page is identical to a visitor's; the cookie did "
                    "nothing")
                page.close()
                print("[browser] and bob is served what a visitor is not")

            context.close()
            seen.append(user)

        browser.close()

    # A contact sheet, so the directory can be read without opening thirty files
    # to work out what they are.
    lines = [
        "# What each of these people sees",
        "",
        "One directory per identity, the same six pages in each. Every identity",
        "is a real login through dex; these are what came back.",
        "",
        "## The pages",
        "",
    ]
    lines += [f"- `{name}` — {why}" for name, _, why in PAGES]
    lines += ["", "## The people", "",
              "Their ACLs are at the top of `server/test/010_fixtures.sql`.", ""]
    lines += [f"- `{who}/`" for who in seen]
    (out / "README.md").write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
