"""Removing an item from a page without breaking the page.

The whole value of this is that it edits a document it does not fully
understand, so most of these tests are about what it must NOT touch.
"""

import re

from kosherd import elementfilter


def bad(text):
    return "lingerie" in text.lower() or "bikini" in text.lower()


def test_a_navigation_item_is_removed_whole():
    html = ('<ul><li><a href="/a">Socks</a></li>'
            '<li><a href="/b">Lingerie</a></li>'
            '<li><a href="/c">Shoes</a></li></ul>')
    out, removed = elementfilter.strip(html, bad)
    assert removed == 1
    assert out == '<ul><li><a href="/a">Socks</a></li><li><a href="/c">Shoes</a></li></ul>'


def test_everything_else_is_byte_identical():
    # A filter that rewrites markup it does not understand breaks sites in
    # ways nobody can debug, so what stays must stay exactly.
    html = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<!-- a comment --><style>a::after{content:"\\2014"}</style></head>'
            '<body class=\'x  y\'><p>Caf&eacute; &#8212; caf&#xe9;</p>'
            '<img src=x.png alt="a &amp; b"><br/>'
            '<script>var a = 1 < 2 && 3 > 2;</script>'
            '<a href="/keep">Keep</a><a href="/go">Lingerie</a>'
            '</body></html>')
    out, removed = elementfilter.strip(html, bad)
    assert removed == 1
    assert out == html.replace('<a href="/go">Lingerie</a>', "")


def test_a_page_with_nothing_to_remove_is_returned_untouched():
    html = "<ul><li><a href='/a'>Socks</a></li></ul>"
    out, removed = elementfilter.strip(html, bad)
    assert removed == 0
    assert out is html  # the same object: nothing was rebuilt


def test_the_address_counts_even_when_the_words_do_not():
    # /b/womens-lingerie labelled "Shop now".
    html = '<a href="/b/womens-lingerie">Shop now</a><a href="/b/socks">Socks</a>'
    out, removed = elementfilter.strip(html, bad)
    assert removed == 1
    assert "Shop now" not in out and "Socks" in out


def test_unclosed_list_items_still_work():
    # Which is most lists in the wild.
    html = "<ul><li><a href=/a>Socks<li><a href=/b>Lingerie<li><a href=/c>Shoes</ul>"
    out, removed = elementfilter.strip(html, bad)
    assert removed == 1
    assert "Lingerie" not in out
    assert "Socks" in out and "Shoes" in out


def test_the_outermost_candidate_goes_with_its_children():
    html = '<li><a href="/x"><span>Womens</span> <b>Lingerie</b></a></li><li>Keep</li>'
    out, removed = elementfilter.strip(html, bad)
    assert removed == 1
    assert out == "<li>Keep</li>"


def test_an_element_too_large_to_be_a_navigation_item_is_kept():
    # Otherwise a page whose <li> wraps half the document disappears.
    html = "<li>" + ("filler " * 3000) + " lingerie</li>"
    out, removed = elementfilter.strip(html, bad)
    assert removed == 0
    assert "filler" in out


def test_an_element_whose_closing_tag_never_comes_is_not_lost():
    html = "<ul><li><a href=/a>Socks"
    out, removed = elementfilter.strip(html, bad)
    assert "Socks" in out


def test_a_page_larger_than_the_cap_is_left_alone():
    html = "<a>Lingerie</a>" + "x" * elementfilter.MAX_PAGE
    out, removed = elementfilter.strip(html, bad)
    assert removed == 0
    assert out is html


def test_the_quick_reject_skips_pages_that_cannot_match():
    # This is what makes it affordable: reparsing a megabyte costs a fifth
    # of a second, and almost no page contains any of these words.
    called = []

    def watched(text):
        called.append(text)
        return bad(text)

    quick = re.compile("lingerie|bikini")
    out, removed = elementfilter.strip("<a>Socks</a>" * 100, watched,
                                       quick_reject=quick)
    assert removed == 0 and called == []

    elementfilter.strip("<a>Lingerie</a>", watched, quick_reject=quick)
    assert called


def test_options_in_a_dropdown_count_as_items():
    html = ('<select><option value="1">All</option>'
            '<option value="2">Lingerie</option></select>')
    out, removed = elementfilter.strip(html, bad)
    assert removed == 1
    assert "Lingerie" not in out and "All" in out


def test_malformed_markup_is_returned_rather_than_mangled():
    for html in ("<a href=<<>>>Lingerie", "<<<>>>", "<a", "</>"):
        out, removed = elementfilter.strip(html, bad)
        assert isinstance(out, str)


def test_an_empty_page_is_not_a_crash():
    assert elementfilter.strip("", bad) == ("", 0)
