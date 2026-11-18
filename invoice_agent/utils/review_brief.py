"""Builds the plain-language brief a person reads when a run waits at human review (the last step of the pipeline).
Pure and deterministic: it only rearranges the flags, uncertainties, invoice and agent rounds already in the state; no LLM calls."""
import re
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel

from invoice_agent.models import Flag, Invoice
from invoice_agent.utils.arithmetic import lines_plus_charges
from invoice_agent.utils.formatting import money

# Acme's reference price as written by the price check: "WidgetA charged $300.00, Acme's price is $250.00".
_PRICE_MESSAGE = re.compile(r"^(?P<sku>.+?) charged .+?, Acme's price is (?P<ref>\$[\d,]+\.\d{2})")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class CheckItem(BaseModel):
    """One thing the reviewer looks at (or, with ok=True, something already verified)."""
    text: str
    field_path: str | None = None
    snippet: str | None = None
    ok: bool = False


class ReviewBrief(BaseModel):
    """Everything the review screen shows: why a person is needed, the agents' view, what to check and what each choice does."""
    headline: str
    why: list[str]
    agents_recommended: str | None
    agents_summary: str | None
    checks: list[CheckItem]
    if_approve: str
    if_reject: str


def _clean(text: str) -> str:
    """Tidy agent-written text: collapse whitespace and swap the em dash for a comma."""
    return " ".join(text.replace("\u2014", ", ").split())


def _shorten(text: str, sentences: int = 2) -> str:
    """Keep only the first couple of sentences of a longer explanation."""
    return " ".join(_SENTENCE_END.split(_clean(text))[:sentences])


def _waiting(state: dict) -> bool:
    """True when the run is held for a person: final says so, or the agents gave up and no outcome exists yet."""
    final, approval = state.get("final"), state.get("approval")
    if final is not None:
        return final.status == "NEEDS_HUMAN_REVIEW"
    return approval is not None and approval.decision == "NEEDS_HUMAN_REVIEW"


def _price_parts(flag: Flag, inv: Invoice) -> dict | None:
    """Pull the item, charged price and Acme's reference price for a price_mismatch flag; None if the message doesn't parse."""
    m = _PRICE_MESSAGE.match(flag.message)
    if not m:
        return None
    index = _item_index(flag.field_path)
    item = inv.items[index] if index is not None and index < len(inv.items) else None
    ref = Decimal(m["ref"].lstrip("$").replace(",", ""))
    return {"sku": m["sku"], "ref": ref, "item": item,
            "charged": item.unit_price if item is not None else None}


def _item_index(path: str | None) -> int | None:
    """The line number inside a path like items[3].unit_price, else None."""
    m = re.match(r"items\[(\d+)\]", path or "")
    return int(m.group(1)) if m else None


def _field_label(path: str) -> str:
    """A readable name for a field path, e.g. items[1].unit_price becomes 'unit price on line 2'."""
    index = _item_index(path)
    name = path.split(".", 1)[1] if "." in path else ("line" if index is not None else path)
    name = name.replace("_", " ")
    return f"{name} on line {index + 1}" if index is not None else name


def _snippet(path: str | None, uncertainties: list, inv: Invoice) -> str | None:
    """The source text to highlight: the reader's raw text for that field, else the line's own raw name."""
    for u in uncertainties:
        if u.field_path == path and u.raw:
            return u.raw
    index = _item_index(path)
    if index is not None and index < len(inv.items):
        return inv.items[index].name_raw
    return None


def _price_check(flag: Flag, inv: Invoice, snippet: str | None) -> CheckItem:
    """Ask whether an off-reference price was agreed, with the per-unit and per-line difference when comparable."""
    parts = _price_parts(flag, inv)
    if parts is None:
        return CheckItem(text=flag.message, field_path=flag.field_path, snippet=snippet)
    item, ref, sku = parts["item"], parts["ref"], parts["sku"]
    charged = parts["charged"]
    if charged is None:
        return CheckItem(text=flag.message, field_path=flag.field_path, snippet=snippet)
    if inv.currency.upper() != "USD":
        # A EUR price can't be subtracted from Acme's USD price, so no difference is computed.
        text = f"Was the {money(charged, inv.currency)} price for {sku} agreed? Acme's price is {money(ref)} (in USD)."
    else:
        diff = abs(charged - ref)
        text = f"Was the {money(charged)} price for {sku} agreed? Acme's price is {money(ref)}"
        if item.qty is not None:
            text += f" (difference {money(diff)} per unit, {money(diff * item.qty)} on this line)."
        else:
            text += f" (difference {money(diff)} per unit)."
    return CheckItem(text=text, field_path=flag.field_path, snippet=snippet)


def _arithmetic_check(flag: Flag, inv: Invoice, snippet: str | None) -> CheckItem:
    """Say what the lines add up to against what the invoice prints, for the total, subtotal or one line."""
    path = flag.field_path or "total"
    cur = inv.currency
    try:
        if path == "total" and inv.total is not None and (computed := lines_plus_charges(inv)) is not None:
            text = f"Check the totals: the lines add up to {money(computed, cur)}, the invoice says {money(inv.total, cur)}."
        elif path == "subtotal" and inv.subtotal is not None:
            lines = sum((i.qty * i.unit_price for i in inv.items if i.qty is not None and i.unit_price is not None), Decimal(0))
            text = f"Check the subtotal: the lines add up to {money(lines, cur)}, the invoice says {money(inv.subtotal, cur)}."
        elif (index := _item_index(path)) is not None and inv.items[index].line_total is not None:
            it = inv.items[index]
            text = (f"Check line {index + 1} ({it.name_raw}): quantity times unit price is "
                    f"{money(it.qty * it.unit_price, cur)}, the invoice says {money(it.line_total, cur)}.")
        else:
            text = f"Check the {_field_label(path)}: the figures on the invoice don't add up."
    except (InvalidOperation, TypeError, IndexError):
        text = f"Check the {_field_label(path)}: the figures on the invoice don't add up."
    return CheckItem(text=text, field_path=path, snippet=snippet)


def _flag_checks(flags: list[Flag], inv: Invoice, uncertainties: list) -> list[CheckItem]:
    """One check per scrutiny flag, ordered by importance (currency, arithmetic, prices, unprinted amounts, size, dates, the rest)."""
    rank = {"non_usd_currency": 0, "arithmetic_mismatch": 1, "price_mismatch": 2, "amount_not_in_source": 3,
            "over_10k": 4, "due_before_or_on_invoice_date": 5, "due_date_missing": 5}
    checks, seen_totals = [], False
    for flag in sorted((f for f in flags if f.severity != "info"), key=lambda f: rank.get(f.code, 6)):
        snippet = _snippet(flag.field_path, uncertainties, inv)
        path = flag.field_path
        if flag.code == "non_usd_currency":
            check = CheckItem(text=f"Confirm the {inv.currency} amounts and the exchange rate with finance before paying.",
                              field_path=path, snippet=snippet)
        elif flag.code == "arithmetic_mismatch":
            # One totals check is enough even when the same invoice reports the total twice.
            if path == "total" and seen_totals:
                continue
            seen_totals = seen_totals or path == "total"
            check = _arithmetic_check(flag, inv, snippet)
        elif flag.code == "price_mismatch":
            check = _price_check(flag, inv, snippet)
        elif flag.code == "amount_not_in_source":
            shown = _amount_text(path, inv)
            check = CheckItem(text=f"Find {shown} on the original document; the reader couldn't see it printed.",
                              field_path=path, snippet=snippet)
        elif flag.code == "over_10k":
            check = CheckItem(text="Large invoice: confirm the vendor and purchase order.", field_path=path, snippet=snippet)
        elif flag.code == "due_before_or_on_invoice_date":
            check = CheckItem(text="Check the due date: it is on or before the invoice date.", field_path=path, snippet=snippet)
        elif flag.code == "due_date_missing":
            check = CheckItem(text="Check the due date: it is missing or couldn't be read.", field_path=path, snippet=snippet)
        else:
            check = CheckItem(text=flag.message, field_path=path, snippet=snippet)
        checks.append(check)
    return checks


def _amount_text(path: str | None, inv: Invoice) -> str:
    """The amount at a field path, formatted as money; a generic phrase when it can't be found."""
    index = _item_index(path)
    try:
        if index is not None:
            value = getattr(inv.items[index], (path or "").split(".")[-1])
        else:
            value = getattr(inv, path or "")
    except (IndexError, AttributeError):
        value = None
    return money(value, inv.currency) if value is not None else "the amount"


def _uncertainty_checks(uncertainties: list, covered: set[str | None]) -> list[CheckItem]:
    """Ask the reviewer to confirm each low or medium confidence reading not already covered by a flag's check."""
    checks = []
    for u in uncertainties:
        if u.confidence == "high" or u.raw is None or u.field_path in covered:
            continue
        checks.append(CheckItem(text=f"Confirm the {_field_label(u.field_path)} reads '{u.raw}'.",
                                field_path=u.field_path, snippet=u.raw))
    return checks


def _open_questions(approval) -> list[str]:
    """Every open question the agents raised (overall and in each round), without repeats."""
    questions = list(approval.open_questions)
    for rnd in approval.rounds:
        questions += rnd.recommendation.open_questions
        if rnd.review is not None:
            questions += rnd.review.open_questions
    seen, unique = set(), []
    for q in map(_clean, questions):
        if q and q.lower() not in seen:
            seen.add(q.lower())
            unique.append(q)
    return unique


def _why(flags: list[Flag], inv: Invoice, approval) -> list[str]:
    """Up to three plain sentences on why a person is needed: the agents' deadlock first, then the money differences."""
    why = []
    if approval is not None and approval.decision == "NEEDS_HUMAN_REVIEW":
        why.append(f"The agents couldn't agree after {max(len(approval.rounds), 1)} rounds.")
    codes = {f.code for f in flags}
    prices = [p for f in flags if f.code == "price_mismatch" and (p := _price_parts(f, inv)) and p["charged"] is not None]
    if "non_usd_currency" in codes:
        why.append(f"The invoice is in {inv.currency}; amounts were not converted to USD.")
    if "arithmetic_mismatch" in codes:
        total_flag = next((f for f in flags if f.code == "arithmetic_mismatch" and f.field_path == "total"), None)
        computed = _safe_computed(inv)
        if total_flag is not None and computed is not None and inv.total is not None:
            why.append(f"The printed total ({money(inv.total, inv.currency)}) doesn't match the line items plus tax ({money(computed, inv.currency)}).")
        else:
            why.append("Some amounts on the invoice don't add up.")
    if prices and inv.currency.upper() == "USD":
        first = prices[0]
        sentence = f"{first['sku']} is charged {money(first['charged'])} on one line; Acme's price is {money(first['ref'])}."
        if len(prices) > 1:
            sentence = sentence.rstrip(".") + f", and {len(prices) - 1} more line{'s' if len(prices) > 2 else ''} differ too."
        why.append(sentence)
    elif prices:
        why.append(f"{len(prices)} line{'s' if len(prices) > 1 else ''} charged a different price from Acme's.")
    if "amount_not_in_source" in codes:
        why.append("Some amounts the reader found aren't printed on the original document.")
    return why[:3]


def _safe_computed(inv: Invoice) -> Decimal | None:
    """Lines plus tax and shipping, or None when it can't be computed."""
    try:
        return lines_plus_charges(inv)
    except InvalidOperation:
        return None


def _agents_view(approval) -> tuple[str | None, str | None]:
    """The agents' leaning (APPROVE, REJECT or None when they deadlocked) and a two-sentence summary of the last VP or analyst reasoning."""
    if approval is None:
        return None, None
    recommended = approval.decision if approval.decision in ("APPROVE", "REJECT") else None
    if not approval.rounds:
        return recommended, None
    last = approval.rounds[-1]
    reasoning = last.review.reasoning if last.review is not None and last.review.reasoning else last.recommendation.reasoning
    return recommended, _shorten(reasoning) or None


def build_review_brief(state: dict) -> ReviewBrief | None:
    """Return the reviewer's brief only for a run waiting at human review; None for any other state."""
    if not _waiting(state):
        return None
    inv: Invoice = state["invoice"]
    flags: list[Flag] = state.get("flags", [])
    uncertainties = state.get("uncertainties", [])
    approval = state.get("approval")
    vendor = inv.vendor or "the vendor"
    amount = money(inv.total, inv.currency)

    why = _why(flags, inv, approval)
    if not why:
        # No concrete flag explains it: fall back to the recorded reason, shortened.
        final = state.get("final")
        why = [_shorten(final.reasoning if final else "A person needs to confirm this invoice before it is paid.", 3)]
    recommended, summary = _agents_view(approval)

    checks = _flag_checks(flags, inv, uncertainties)
    covered = {c.field_path for c in checks}
    checks += _uncertainty_checks(uncertainties, covered)
    checks += [CheckItem(text=q) for q in (_open_questions(approval) if approval is not None else [])]
    if not any(f.severity == "must_reject" for f in flags):
        matches = "The items are in Acme's inventory with enough stock." if inv.currency.upper() != "USD" \
            else ("The other lines match Acme's prices and stock." if any(f.code == "price_mismatch" for f in flags)
                  else "Everything else matches Acme's prices and stock.")
        checks.append(CheckItem(text=matches, ok=True))

    return ReviewBrief(
        headline=f"Waiting for you: {amount} to {vendor}",
        why=why, agents_recommended=recommended, agents_summary=summary, checks=checks,
        if_approve=f"{amount} is paid to {vendor}.",
        if_reject="Nothing is paid; the invoice is logged as rejected with your note.",
    )
