"""Turns an ApprovalContext and earlier rounds into the plain-text briefing the analyst and VP agents read.
Pure formatting, no LLM calls; used by agents/analyst and agents/vp in the approval stage.
Vendor-derived text is fenced in <invoice_data> and earlier agent text in <prior_rounds> (prompt-injection defence);
the policy is not here, it goes in the agents' system prompts.
"""
from invoice_agent.agents.approval.loop import ApprovalContext
from invoice_agent.models import Invoice, Recommendation, Round, VPReview


def _fence(tag: str, lines: list[str]) -> list[str]:
    """Wrap data lines in <tag>...</tag>; any "<" inside the data becomes "‹", so it cannot fake or close a fence."""
    body = "\n".join(lines).replace("<", "‹")
    return [f"<{tag}>", body, f"</{tag}>"]


def _v(value) -> str:
    """Show a missing value as "(missing)" so the agent never mistakes a blank for zero."""
    return "(missing)" if value is None or value == "" else str(value)


def _invoice_lines(inv: Invoice) -> list[str]:
    """Header fields and one line per item."""
    out = [
        f"Invoice number: {_v(inv.invoice_number)}",
        f"Vendor: {_v(inv.vendor)}",
        f"Date: {_v(inv.date)}   Due date: {_v(inv.due_date)}",
        f"Currency: {inv.currency}",
        f"Subtotal: {_v(inv.subtotal)}   Tax: {_v(inv.tax)}   Shipping: {_v(inv.shipping)}   Total: {_v(inv.total)}",
        f"Payment terms: {_v(inv.payment_terms)}",
    ]
    if inv.notes:
        out.append(f"Notes: {inv.notes}")
    out.append("Line items:")
    if not inv.items:
        out.append("  (none)")
    for i, it in enumerate(inv.items):
        # Show the SKU match next to the invoice's own wording so a mismatch is visible.
        sku = it.sku or (f"no exact match, closest: {it.sku_suggestion}" if it.sku_suggestion else "no inventory match")
        line = (f"  [{i}] {it.name_raw} (SKU: {sku}) qty {_v(it.qty)} x unit price {_v(it.unit_price)}"
                f" = line total {_v(it.line_total)}")
        if it.note:
            line += f"  note: {it.note}"
        out.append(line)
    return out


def _recommendation_lines(rec: Recommendation, indent: str = "  ") -> list[str]:
    """Decision, reasoning, evidence, checklist and open questions of one recommendation."""
    out = [f"{indent}Decision: {rec.decision}", f"{indent}Reasoning: {rec.reasoning}"]
    out += [f"{indent}Evidence: {e}" for e in rec.evidence]
    if rec.checklist:
        out += [f"{indent}Checklist {k}: {v}" for k, v in rec.checklist.items()]
    out += [f"{indent}Open question: {q}" for q in rec.open_questions]
    return out


def _review_lines(review: VPReview, indent: str = "  ") -> list[str]:
    """Verdict, reasoning and feedback of one VP review."""
    out = [f"{indent}VP verdict: {review.verdict} (final decision: {_v(review.final_decision)})",
           f"{indent}VP reasoning: {review.reasoning}"]
    if review.feedback:
        out.append(f"{indent}VP feedback: {review.feedback}")
    out += [f"{indent}VP open question: {q}" for q in review.open_questions]
    return out


def render_context(ctx: ApprovalContext, rounds: list[Round], recommendation: Recommendation | None = None) -> str:
    """Render the fenced invoice data (invoice, flags, uncertainties), reviewed earlier rounds, and the draft to review (VP)."""
    # Flags and uncertainties quote item names and raw invoice text, so they are vendor data too.
    data = ["## Invoice", *_invoice_lines(ctx.invoice), "", "## Validation flags"]
    data += [f"- [{f.severity}] {f.code}: {f.message}" + (f" (field: {f.field_path})" if f.field_path else "")
             for f in ctx.flags] or ["- none"]
    data += ["", "## Where the extraction was uncertain"]
    data += [f"- {u.field_path} ({u.confidence} confidence): {u.reason}"
             + (f", source text {u.raw!r}, read as {u.normalized!r}" if u.raw or u.normalized else "")
             for u in ctx.uncertainties] or ["- none"]
    parts = _fence("invoice_data", data)
    # Only rounds the VP has already reviewed are history; the VP's current draft is shown separately below.
    reviewed = [r for r in rounds if r.review is not None]
    if reviewed:
        history = []
        for r in reviewed:
            history += [f"Round {r.number}:", *_recommendation_lines(r.recommendation), *_review_lines(r.review)]
        parts += ["", "## Previous rounds", *_fence("prior_rounds", history)]
    if recommendation is not None:
        # The draft may quote vendor text, so it gets the same neutralizing fence.
        parts += ["", "## Analyst recommendation to review",
                  *_fence("analyst_recommendation", _recommendation_lines(recommendation))]
    return "\n".join(parts)
