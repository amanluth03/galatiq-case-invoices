"""Marks the agents' uncertain spots in the original invoice text for the review screen's source-document pane.
Vendor text is untrusted, so every character is HTML-escaped; only our own <mark> tags are added. Each mark is
focusable and carries its reason in data-reason, which live.js shows as a tooltip on hover, focus or tap.
"""
from markupsafe import Markup, escape

from invoice_agent.models import Uncertainty


def highlight(raw_text: str, uncertainties: list[Uncertainty]) -> Markup:
    """Escape the whole text and wrap the first occurrence of each uncertainty's snippet in a confidence-tinted,
    focusable <mark> whose data-reason says why the reader was unsure."""
    # Find spans on the unescaped text, then escape each piece: a snippet can never split an HTML entity.
    spans: list[tuple[int, int, Uncertainty]] = []
    for u in uncertainties:
        if not u.raw:
            continue
        start = raw_text.find(u.raw)
        if start < 0:
            continue  # e.g. a defaulted currency has no source snippet to point at
        end = start + len(u.raw)
        # Overlapping marks would nest or interleave, so the later snippet is skipped.
        if any(start < e and s < end for s, e, _ in spans):
            continue
        spans.append((start, end, u))
    out, pos = [], 0
    for start, end, u in sorted(spans, key=lambda s: s[0]):
        out.append(escape(raw_text[pos:start]))
        out.append(Markup('<mark class="conf-{}" tabindex="0" data-reason="{}">{}</mark>').format(
            u.confidence, u.reason, raw_text[start:end]))
        pos = end
    out.append(escape(raw_text[pos:]))
    return Markup("").join(out)
