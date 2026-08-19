"""reStructuredText, and the three settings that make it safe to offer.

docutils is not dangerous by accident; it is a document processor with
features a document processor is expected to have, and two of them are
catastrophic when the document was written by one user and is read by another.
Every assertion below was a measurement first.

The one worth reading twice is the `docutils.conf` case. A config file in the
working directory *overrides* the settings passed programmatically, so the two
flags above it can be set to False and mean nothing.
"""

from __future__ import annotations

import pytest

from fswiki_core import naming, render

RST = "text/x-rst"

pytestmark = pytest.mark.skipif(
    not any(RST in b.content_types for b in render.available()),
    reason="no reStructuredText backend installed")


def r(text: str) -> str:
    return render.render(text, content_type=RST).html


# --- it renders -------------------------------------------------------------

def test_a_document_renders():
    html = r("Title\n=====\n\nSome *emphasis* and a paragraph.\n")
    assert "<h1" in html and "<em>" in html
    assert "Some" in html


def test_the_things_people_choose_rst_for_survive():
    """Directives are the reason to want this rather than markdown."""
    html = r("""Title
=====

.. note::

   Mind the gap.

.. code-block:: python

   def f():
       return 1
""")
    assert "Mind the gap." in html
    assert "<aside" in html, "the admonition lost its structure to the sanitiser"
    assert "return 1" in html


def test_a_table_survives():
    html = r("""
+---+---+
| a | b |
+===+===+
| 1 | 2 |
+---+---+
""")
    assert "<table" in html and "<td" in html


def test_malformed_input_is_a_page_not_an_exception():
    """A wiki page with a typo in it is still a page. docutils would otherwise
    raise, or render its own complaints into what the reader sees."""
    html = r("Title\n===\n\n`unclosed interpreted text\n\n.. nonexistent:: x\n")
    assert isinstance(html, str)
    assert "System Message" not in html and "ERROR/" not in html


# --- and it does not read the server's filesystem ---------------------------

def test_include_does_not_read_a_server_file(tmp_path):
    """The one that would have been a hole. With docutils' defaults,
    `.. include::` opens the named path and puts its contents in the page --
    arbitrary server-file disclosure, written by one user and read by another.
    """
    victim = tmp_path / "victim.txt"
    victim.write_text("SUPER-SECRET-SERVER-CONTENTS\n")
    html = r(f"Page\n====\n\n.. include:: {victim}\n")
    assert "SUPER-SECRET-SERVER-CONTENTS" not in html


def test_and_the_sanitiser_would_not_have_caught_it(tmp_path):
    """Why the setting is load-bearing rather than defence in depth: an
    included file arrives as text nodes, not as tags, so nh3 has no reason to
    touch it. This asserts the *sanitiser's* limit, so nobody later decides the
    parser setting is redundant."""
    from fswiki_core.render import safety
    assert "SUPER-SECRET" in safety.clean("<p>SUPER-SECRET-SERVER-CONTENTS</p>")


def test_raw_html_does_not_reach_the_reader():
    html = r("Page\n====\n\n.. raw:: html\n\n   <script>alert(1)</script>\n")
    assert "<script" not in html and "alert(1)" not in html


def test_a_docutils_conf_in_the_working_directory_cannot_re_enable_them(
        tmp_path, monkeypatch):
    """The surprising one. docutils reads `docutils.conf` from the working
    directory and lets it override `settings_overrides`, so without
    `_disable_config` a file sitting next to the server silently restores
    arbitrary file reads. Measured: the include leaked again with both flags
    still set to False."""
    victim = tmp_path / "victim.txt"
    victim.write_text("SUPER-SECRET-SERVER-CONTENTS\n")
    (tmp_path / "docutils.conf").write_text(
        "[general]\nfile_insertion_enabled: yes\nraw_enabled: yes\n")
    monkeypatch.chdir(tmp_path)

    html = r(f"Page\n====\n\n.. include:: {victim}\n")
    assert "SUPER-SECRET-SERVER-CONTENTS" not in html


# --- how it is chosen -------------------------------------------------------

def test_the_content_type_selects_it():
    assert render.render("Hi\n==\n", content_type=RST).renderer.startswith("docutils/")


def test_markdown_is_still_markdown():
    """Adding a format must not move an existing one."""
    assert not render.render(
        "# Hi", content_type="text/markdown").renderer.startswith("docutils/")


def test_an_rst_filename_round_trips_to_the_rst_content_type():
    """The mount shows `page.rst` and the schema stores `text/x-rst`; a format
    nobody can name in a filename is a format nobody can write."""
    assert naming.parse_filename("page.rst") == ("page", RST)
    assert naming.filename("page", RST, is_folder=False) == "page.rst"


def test_the_renderer_id_carries_the_docutils_version():
    """It is part of the cache key, so an upgrade must miss rather than serve
    output the running code would not produce."""
    import docutils
    assert docutils.__version__ in render.render("Hi\n==\n", content_type=RST).renderer
