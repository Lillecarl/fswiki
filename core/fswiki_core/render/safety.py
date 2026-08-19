"""Sanitising. Runs over every backend's output, always.

Not pluggable, on purpose. A backend is chosen for how it reads markup; this
decides what a reader's browser is allowed to execute, and those are not the
same kind of decision. Letting a backend opt out of sanitising would make the
safety of the wiki depend on which engine an operator picked, which is exactly
the coupling the plugin seam exists to avoid.

It is also cheap enough not to argue about: 0.59 ms on a 5.4 kB page, against
0.6 ms per kB to render it in the first place.

Two layers, because a parser and a sanitiser fail differently. Backends are
asked to disable raw HTML, so hostile markup never becomes tags at all; this
catches what the parser was never asked about — a `javascript:` href, an
`onclick`, a plugin that emits something unexpected.
"""

from __future__ import annotations

# Enough for prose. Anything not listed is dropped rather than escaped, so a
# stray tag disappears instead of being shown to the reader as source.
TAGS = {
    # `s` beside `del`, because the two markdown engines disagree about which
    # one `~~struck~~` is: markdown-it emits <s>, mistune emits <del>. Without
    # it the tag is unwrapped and the text renders unstruck -- silently, which
    # is the failure mode an allowlist has and a blocklist does not.
    "p", "br", "hr", "em", "strong", "del", "ins", "s", "sub", "sup", "code", "pre",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "a", "img", "span", "div",
    # Structure that reStructuredText emits and markdown does not. Both are
    # inert -- no scripting surface, no navigation -- and without them nh3
    # unwraps every `.. note::` into an undistinguished paragraph, which loses
    # the one thing an admonition is for.
    "section", "aside",
    # `tt` is obsolete HTML, and docutils emits it for an inline expression it
    # could not convert. Keeping it keeps failed maths visibly maths instead
    # of running it into the prose. See render.maths.
    "tt",
}

ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    # Heading ids are how a deep link into a page works at all.
    "h1": {"id"}, "h2": {"id"}, "h3": {"id"},
    "h4": {"id"}, "h5": {"id"}, "h6": {"id"},
    "th": {"align"}, "td": {"align"},
    "code": {"class"}, "pre": {"class"}, "span": {"class"}, "div": {"class"},
    "ol": {"start"},
    # `class` carries which kind of admonition it is; `id` is what a deep link
    # into a section needs. Both are inert, and div/span already allow class.
    "aside": {"class"}, "section": {"id", "class"},
    # `class="math"` is what says an expression failed to convert rather than
    # that an author wrote monospaced text.
    "tt": {"class"},
}

# MathML, which is what an expression renders to. See render.maths.
#
# It needs listing at all because it is *foreign content* to the HTML parser:
# an element the sanitiser does not know is dropped whole here, children and
# text with it, rather than unwrapped. Measured before this list existed --
# 206 bytes of MathML in, 0 bytes out, not even the numbers.
#
# The surface is small and inert. There is no scripting element, nothing that
# navigates, and no `foreignObject` to smuggle HTML through, which is what
# makes this a much smaller decision than allowing SVG (issue #6). It is the
# union of what our two producers emit: latex2mathml for markdown, and
# docutils' own converter for reStructuredText.
#
# Two omissions are deliberate, and both were measured rather than assumed:
#
#   `annotation-xml` is absent. With `encoding="text/html"` it becomes an HTML
#   integration point, which is the classic mutation-XSS route through a
#   sanitiser. Left out, nh3 drops the element and the `<script>` inside it.
#
#   `href` is absent from every element. `\href{...}{...}` puts one on an
#   `<mrow>`, and latex2mathml will happily write
#   `<mrow href="javascript:alert(1)">`. Maths is notation, not navigation.
MATHML_TAGS = {
    "math", "mrow", "mi", "mn", "mo", "mtext", "mspace",
    "mfrac", "msqrt", "mroot", "mstyle", "mpadded", "mphantom", "menclose",
    "msub", "msup", "msubsup", "munder", "mover", "munderover",
    "mtable", "mtr", "mtd",
    # docutils uses this to show an expression it could not parse.
    "merror",
}

# Presentation only: sizes, alignment, colours and which glyphs stretch.
# Nothing here is a URL, so nothing here goes through `url_schemes`, and
# nothing here needs to.
MATHML_ATTRIBUTES = {
    "math": {"display", "xmlns", "class"},
    "menclose": {"notation"},
    "mfrac": {"linethickness", "numalign"},
    "mi": {"mathvariant"},
    "mn": {"mathvariant"},
    "mtext": {"mathvariant"},
    "mo": {"accent", "fence", "form", "largeop", "lspace", "rspace",
           "maxsize", "minsize", "movablelimits", "stretchy", "symmetric"},
    "mover": {"accent"},
    "munder": {"accentunder"},
    "munderover": {"accent", "accentunder"},
    "mpadded": {"depth", "height", "lspace", "voffset", "width",
                "mathbackground"},
    "mspace": {"depth", "height", "linebreak", "width", "mathbackground"},
    "mstyle": {"displaystyle", "mathbackground", "mathcolor", "mathsize",
               "mathvariant", "scriptlevel"},
    "mtable": {"columnalign", "columnspacing", "displaystyle", "rowlines",
               "rowspacing"},
    "mtd": {"columnalign", "columnspan", "rowspan"},
}

# Wiki links are relative paths under a reserved prefix rather than a custom
# scheme, so nothing has to be allowlisted for them — see render.links. `data:`
# is absent deliberately: it is how an image becomes a script in some browsers.
URL_SCHEMES = {"http", "https", "mailto"}


class SanitiserUnavailable(RuntimeError):
    """nh3 is not installed, and rendering without it is not on offer."""


def clean(html: str) -> str:
    """Strip anything a reader's browser should not be given."""
    try:
        import nh3
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise SanitiserUnavailable(
            "nh3 is required to render: serving unsanitised HTML written by "
            "one user to another user's browser is not a supported mode"
        ) from exc

    return nh3.clean(
        html,
        tags=TAGS | MATHML_TAGS,
        attributes={**ATTRIBUTES, **MATHML_ATTRIBUTES},
        url_schemes=URL_SCHEMES,
        strip_comments=True,
        link_rel="noopener noreferrer",
    )
