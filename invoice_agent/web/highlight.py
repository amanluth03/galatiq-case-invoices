"""Marks the agents' uncertain spots in the original invoice text for the review screen's source-document pane.
Vendor text is untrusted, so every character is HTML-escaped; only our own <mark> tags are added. Each mark is
focusable and carries its reason in data-reason, which live.js shows as a tooltip on hover, focus or tap. The review
brief's "Show in invoice" links find their spot through data-anchor keys on the marks (see anchor_key).
"""
import hashlib

from markupsafe import Markup, escape

from invoice_agent.models import Uncertainty


def anchor_key(snippet: str) -> str:
    """A short, HTML-safe key for a source snippet, shared by the brief's link and the mark it jumps to."""
    return "a" + hashlib.sha256(snippet.encode()).hexdigest()[:10]


def highlight(raw_text: str, uncertainties: list[Uncertainty], anchors: list[str] = ()) -> Markup:
    """Escape the whole text and wrap the first occurrence of each uncertainty's snippet in a confidence-tinted,
    focusable <mark> whose data-reason says why the reader was unsure.
    anchors: snippets the review brief links to; each found one gets its key in a mark's data-anchor."""
    # Find spans on the unescaped text, then escape each piece: a snippet can never split an HTML entity.
    spans: list[tuple[int, int, Uncertainty | None]] = []
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
    keys: dict[int, list[str]] = {}  # span index -> anchor keys it answers to
    for snippet in dict.fromkeys(a for a in anchors if a):
        start = raw_text.find(snippet)
        if start < 0:
            continue
        end = start + len(snippet)
        hit = next((i for i, (s, e, _) in enumerate(spans) if start < e and s < end), None)
        if hit is None:
            # Not already highlighted (e.g. a line's item name): add a plain mark that only lights up when jumped to.
            spans.append((start, end, None))
            hit = len(spans) - 1
        # An overlapping uncertainty mark stands in for the snippet, so the link still lands on the right spot.
        keys.setdefault(hit, []).append(anchor_key(snippet))
    out, pos = [], 0
    for i, (start, end, u) in sorted(enumerate(spans), key=lambda s: s[1][0]):
        out.append(escape(raw_text[pos:start]))
        anchor = Markup(' data-anchor="{}"').format(" ".join(keys[i])) if i in keys else Markup("")
        if u is None:
            out.append(Markup('<mark class="snip" tabindex="-1"{}>{}</mark>').format(anchor, raw_text[start:end]))
        else:
            out.append(Markup('<mark class="conf-{}" tabindex="0" data-reason="{}"{}>{}</mark>').format(
                u.confidence, u.reason, anchor, raw_text[start:end]))
        pos = end
    out.append(escape(raw_text[pos:]))
    return Markup("").join(out)
