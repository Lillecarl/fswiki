"""What a render backend is, and how one is chosen.

The backend converts markup to HTML and does nothing else. Everything specific
to this wiki — resolving `[[wikilinks]]`, refusing raw HTML, deciding which
links a given reader may follow — happens on either side of it, in code that
does not change when the backend does.

That split is the point rather than a tidiness preference. The link-graph leak
described in docs/rendering.md is a security property, and a security property
that each backend has to reimplement is one that some backend will get wrong.
**The backend is pluggable precisely because the invariants are not.**

Selection is by `content_type`, which the schema already carries per revision,
so a second markup language is a backend registration rather than a change
anywhere else.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Protocol, runtime_checkable

# What the passes on either side of the backend emit, versioned. It travels in
# the renderer id and therefore in the cache key, because a cached body is only
# reusable if the whole pipeline that produced it is the same.
#
#   2: the sanitiser began allowing <section> and <aside>, for docutils.
#   3: and <s>, which markdown-it emits for `~~struck~~` and which was being
#      unwrapped -- the text rendered, unstruck, with nothing to indicate it.
#   4: and MathML, which is foreign content and was therefore being dropped
#      whole -- 206 bytes of it in, 0 out. See render.maths.
PIPELINE_VERSION = 4


@runtime_checkable
class Backend(Protocol):
    """Markup in, HTML out. No state, no I/O, no opinions about fswiki."""

    #: Stable identifier, used in the cache key. Never reuse one.
    name: str
    #: The underlying library's version, so an upgrade misses the cache
    #: rather than serving output the current code would not produce.
    version: str
    #: The content types this backend claims.
    content_types: tuple[str, ...]
    #: Every option that changes what `to_html` emits, as plain data.
    #:
    #: Digested into the renderer id, and therefore into the cache key. The
    #: point is that it cannot be forgotten: `version` is the *library's*, so
    #: turning a plugin on or off changes the output while leaving the version
    #: alone -- and a cache keyed on the version alone would go on serving what
    #: the old configuration produced. Declaring the options rather than a
    #: hand-written token means the key moves whether or not anyone remembers
    #: that it should.
    options: dict

    def to_html(self, text: str) -> str:
        """Convert `text`. Must not emit raw HTML from the source.

        "Must not" is a request, not a guarantee we rely on: the sanitiser
        runs over the output regardless. A backend that honours it saves the
        sanitiser some work and nothing else.
        """


class UnknownBackend(LookupError):
    """No backend is registered under that name, or for that content type."""


_by_name: dict[str, Backend] = {}
_by_type: dict[str, list[Backend]] = {}


def config_digest(backend: Backend) -> str:
    """A short stable fingerprint of a backend's options, or "" if it has none.

    Canonical JSON so that key order and whitespace cannot move it, truncated
    because this is a cache key rather than a signature: eight hex characters
    is 4 billion configurations, against the handful any deployment has.
    """
    options = getattr(backend, "options", None)
    if not options:
        return ""
    canonical = json.dumps(options, sort_keys=True, default=repr).encode()
    return hashlib.sha256(canonical).hexdigest()[:8]


def register(backend: Backend) -> Backend:
    """Add a backend. Returns it, so it can be used as a decorator.

    Registering the same name twice replaces the earlier one, which is what
    makes an out-of-tree backend able to override a shipped one.
    """
    _by_name[backend.name] = backend
    for content_type in backend.content_types:
        entries = _by_type.setdefault(content_type, [])
        # Newest registration wins the default slot for its content type,
        # while staying reachable by name.
        _by_type[content_type] = [backend] + [
            b for b in entries if b.name != backend.name
        ]
    return backend


def available() -> list[Backend]:
    """Every registered backend, in registration order by name."""
    return sorted(_by_name.values(), key=lambda b: b.name)


def get(content_type: str = "text/markdown", name: str | None = None) -> Backend:
    """The backend to use, by explicit name or by content type.

    `name` beats everything, then $FSWIKI_RENDERER, then whatever registered
    most recently for the content type. The environment variable is there so a
    deployment can pin an engine without every caller passing it down.
    """
    wanted = name or os.environ.get("FSWIKI_RENDERER") or None
    if wanted:
        backend = _by_name.get(wanted)
        if backend is None:
            known = ", ".join(sorted(_by_name)) or "none"
            raise UnknownBackend(f"no render backend named {wanted!r} (have: {known})")
        if content_type not in backend.content_types:
            raise UnknownBackend(
                f"backend {wanted!r} does not handle {content_type!r}")
        return backend

    for backend in _by_type.get(content_type, ()):
        return backend
    known = ", ".join(sorted(_by_type)) or "none"
    raise UnknownBackend(
        f"no render backend for {content_type!r} (have: {known})")
