"""Tests for highlight(): the review screen's source-document view must escape vendor text and mark uncertain snippets."""
from invoice_agent.models import Uncertainty
from invoice_agent.web.highlight import highlight


def unc(raw, confidence="medium", reason="looks like a letter O, not a zero"):
    """An Uncertainty on the invoice date with the given source snippet."""
    return Uncertainty(field_path="date", raw=raw, confidence=confidence, reason=reason)


def test_highlight_escapes_html():
    """Vendor text is escaped (no live <script>) and the uncertain snippet is wrapped in a focusable, confidence-tinted
    mark that carries its reason for the tooltip."""
    out = str(highlight("<script>x</script> & 26-Jan-2O26", [unc("26-Jan-2O26")]))
    assert "&lt;script&gt;" in out and "&amp;" in out
    assert "<script>" not in out
    assert '<mark class="conf-medium" tabindex="0" data-reason="looks like a letter O, not a zero">26-Jan-2O26</mark>' in out


def test_highlight_missing_snippet_is_skipped():
    """A snippet that is not in the text (or is empty) is skipped without error; the text comes back escaped."""
    out = str(highlight("Total: $5 & tax", [unc("not there"), unc(None), unc("")]))
    assert out == "Total: $5 &amp; tax"


def test_highlight_overlapping_snippet_is_skipped():
    """A second snippet overlapping an already-marked one is skipped, so marks never nest or interleave."""
    out = str(highlight("Due: 2026-02-01", [unc("2026-02-01", "low"), unc("02-01")]))
    assert out.count("<mark") == 1 and 'class="conf-low"' in out


def test_highlight_escapes_reason_in_attribute():
    """A reason containing quotes or tags cannot break out of the data-reason attribute."""
    out = str(highlight("abc", [unc("abc", reason='"><script>')]))
    assert "<script>" not in out and "&#34;&gt;&lt;script&gt;" in out


def test_highlight_anchors_mark_brief_snippets():
    """A brief snippet gets its own plain mark keyed by anchor_key; one overlapping an uncertainty mark adds its key
    to that mark instead, and a snippet not in the text is skipped."""
    from invoice_agent.web.highlight import anchor_key
    out = str(highlight("WidgetA x4 on 26-Jan-2O26", [unc("26-Jan-2O26")], ["WidgetA", "2O26", "nowhere"]))
    assert f'<mark class="snip" tabindex="-1" data-anchor="{anchor_key("WidgetA")}">WidgetA</mark>' in out
    assert f'data-anchor="{anchor_key("2O26")}">26-Jan-2O26</mark>' in out and out.count("<mark") == 2
