"""What every render backend must do, whichever one it is.

A plugin seam is a promise about behaviour, and a promise nobody checks is a
comment. So this runs the same cases against **every registered backend** and
fails if any of them disagrees where the wiki depends on agreement.

It deliberately does not compare HTML byte for byte. Engines differ on
whitespace, on attribute order, on whether a paragraph wraps a lone image, and
none of that matters. What matters is the handful of properties the rest of
fswiki relies on:

* raw HTML from a document never reaches a reader;
* a `javascript:` URL never survives;
* a wiki link becomes an anchor under the reserved prefix and nothing else;
* a forbidden link is indistinguishable from a missing one;
* the renderer id changes when the backend does, because it is a cache key.

    nix-shell -p 'python3.withPackages(...)' --run 'python3 core/test/conformance.py'
"""

from __future__ import annotations

import sys

sys.path.insert(0, "core")

from fswiki_core import render                       # noqa: E402
from fswiki_core.render import links, registry       # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} -- {detail}")


def markdown_backends() -> list:
    return [b for b in render.available() if "text/markdown" in b.content_types]


def main() -> int:
    backends = markdown_backends()
    if not backends:
        print("no markdown backend registered — install markdown-it-py or mistune")
        return 1

    print(f"== {len(backends)} markdown backend(s): "
          f"{', '.join(b.name for b in backends)}")

    for backend in backends:
        n = backend.name
        print(f"\n== {n} {backend.version}")

        def r(text: str) -> str:
            return render.render(text, backend=n).html

        # 1. Raw HTML in a document is written by one user and read by another.
        html = r("<script>alert(1)</script>\n\n<div onclick='x'>hi</div>")
        check(f"[{n}] no script survives", "<script" not in html, html)
        # The tag itself must never materialise. Both engines escape it into
        # visible text, where the word "onclick" is harmless and searching for
        # it would fail the check for the wrong reason.
        check(f"[{n}] no raw tag survives", "<div" not in html, html)

        # 2. A URL scheme the sanitiser does not allow.
        # The check is on the href, not on the string. markdown-it declines to
        # build the link at all and leaves the source as visible text, which is
        # safe and arguably clearer; searching for the word would fail that.
        html = r("[click](javascript:alert(1))")
        check(f"[{n}] no javascript: href survives",
              'href="javascript' not in html.replace("&#", ""), html)

        # 3. Wiki links, which no backend knows about — the pre-pass does.
        html = r("see [[public/welcome]]")
        check(f"[{n}] a wikilink becomes a reserved-prefix anchor",
              f'href="{links.PREFIX}root.public.welcome"' in html, html)
        check(f"[{n}] and is counted as unresolved",
              links.unresolved(html) == 1, html)

        # 4. The property the whole link-leak argument rests on.
        forbidden = links.resolve(r("[[secret/plans|Label]]"), lambda p: None)
        missing = links.resolve(r("[[gone/away|Label]]"), lambda p: None)
        check(f"[{n}] forbidden and missing render identically",
              forbidden == missing, f"{forbidden!r} vs {missing!r}")
        check(f"[{n}] and neither is a link", "<a" not in forbidden, forbidden)
        check(f"[{n}] while the text is kept", "Label" in forbidden, forbidden)

        allowed = links.resolve(r("[[public/welcome]]"),
                                lambda p: f"/w/{p}")
        check(f"[{n}] an allowed link becomes a real href",
              'href="/w/root.public.welcome"' in allowed, allowed)
        check(f"[{n}] with nothing left unresolved",
              links.unresolved(allowed) == 0, allowed)

        # 5. Ordinary markdown still works, or the backend is not a backend.
        html = r("# Title\n\ntext with **bold**\n\n- one\n- two")
        for fragment in ("<h1", "<strong", "<ul", "<li"):
            check(f"[{n}] renders {fragment}>", fragment in html, html)

        # 6. The renderer id is a cache key, so it must name the backend.
        page = render.render("x", backend=n)
        check(f"[{n}] the renderer id names the backend and version",
              page.renderer.startswith(f"{n}/{backend.version}")
              and f"+fswiki{registry.PIPELINE_VERSION}" in page.renderer,
              page.renderer)

    print("\n== the seam itself")
    ids = {render.render("x", backend=b.name).renderer for b in backends}
    check("every backend produces a distinct renderer id", len(ids) == len(backends),
          str(ids))

    try:
        render.render("x", backend="no-such-engine")
        check("an unknown backend is refused", False, "no error raised")
    except render.UnknownBackend:
        check("an unknown backend is refused", True)

    try:
        render.render("x", content_type="application/x-nonsense")
        check("an unhandled content type is refused", False, "no error raised")
    except render.UnknownBackend:
        check("an unhandled content type is refused", True)

    plain = render.render("a < b", content_type="text/plain")
    check("text/plain goes to its own backend", plain.renderer.startswith("plain/"),
          plain.renderer)
    check("and is escaped, not interpreted", "&lt;" in plain.html, plain.html)

    print(f"\n  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
