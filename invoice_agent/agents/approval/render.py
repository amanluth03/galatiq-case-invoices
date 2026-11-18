"""Turns an ApprovalContext and earlier rounds into the plain-text briefing the analyst and VP agents read.
Pure formatting, no LLM calls; used by agents/analyst and agents/vp in the approval stage.
"""
from invoice_agent.agents.approval.loop import ApprovalContext
from invoice_agent.models import Invoice, Recommendation, Round, VPReview


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
    """Render invoice, flags, uncertainties, the policy and reviewed earlier rounds; plus the draft to review (VP)."""
    parts = ["## Invoice", *_invoice_lines(ctx.invoice), "", "## Validation flags"]
    parts += [f"- [{f.severity}] {f.code}: {f.message}" + (f" (field: {f.field_path})" if f.field_path else "")
              for f in ctx.flags] or ["- none"]
    parts += ["", "## Where the extraction was uncertain"]
    parts += [f"- {u.field_path} ({u.confidence} confidence): {u.reason}"
              + (f" — source text {u.raw!r}, read as {u.normalized!r}" if u.raw or u.normalized else "")
              for u in ctx.uncertainties] or ["- none"]
    # Only rounds the VP has already reviewed are history; the VP's current draft is shown separately below.
    reviewed = [r for r in rounds if r.review is not None]
    if reviewed:
        parts += ["", "## Previous rounds"]
        for r in reviewed:
            parts += [f"Round {r.number}:", *_recommendation_lines(r.recommendation), *_review_lines(r.review)]
    if recommendation is not None:
        parts += ["", "## Analyst recommendation to review", *_recommendation_lines(recommendation)]
    parts += ["", "## Approval policy", ctx.policy.strip()]
    return "\n".join(parts)
