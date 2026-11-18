"""The invoice story: one invoice's trip through the pipeline told as 7 plain-language steps
(read -> ingestion agent -> inventory and rules -> analyst -> VP -> guardrail -> decision).
build_story is the single builder behind both the live view (while a run is PROCESSING) and a finished invoice's
page, so the two always tell the same story. It is pure: it only reads the state it is given.
"""
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from invoice_agent.models import ApprovalOutcome, Flag, Round
from invoice_agent.utils.formatting import money, nice_date, quantity  # noqa: F401 (re-exported for app.py)
from invoice_agent.utils.guardrail import HUMAN_REVIEW_REASON

# Stages a run reports before any graph node has finished: nothing in its stored state belongs to this run yet.
NOT_STARTED = {None, "", "started", "queued"}
QUEUED = "queued"

STATUS_LABELS = {
    "PAID": "Paid", "REJECTED": "Rejected", "NEEDS_HUMAN_REVIEW": "Waiting for you",
    "DUPLICATE": "Duplicate", "ERROR": "Error", "PROCESSING": "Running",
}
DECIDED_BY_LABELS = {
    "agents": "the analyst and VP agents", "guardrail": "the guardrail (a hard rule)",
    "human": "you (a reviewer)", "system": "the system",
}
FORMAT_LABELS = {"pdf": "PDF", "txt": "Text invoice", "json": "JSON file", "csv": "CSV file", "xml": "XML file"}
# Plain names for invoice fields, used for "unsure about: ..." and for field paths inside flag messages.
FIELD_WORDS = {
    "invoice_number": "invoice number", "vendor": "vendor", "date": "invoice date", "due_date": "due date",
    "payment_terms": "payment terms", "currency": "currency", "subtotal": "subtotal", "tax": "tax",
    "shipping": "shipping", "total": "total", "notes": "notes", "items": "line items",
    "name": "item name", "qty": "quantity", "unit_price": "unit price", "line_total": "line total", "note": "note",
}
# Validation codes that mean "a stock problem", so "All items are in stock" is only said when none is raised.
_STOCK_CODES = {"unknown_item", "zero_stock_item", "exceeds_stock"}
# Blocking reasons phrased for "A hard rule is broken (2 items aren't in inventory)".
# A pair is (singular, plural) and gets a count; a plain string is a one-off fact.
_BLOCKING_PHRASES: dict[str, tuple[str, str] | str] = {
    "unknown_item": ("item isn't in inventory", "items aren't in inventory"),
    "zero_stock_item": ("item has zero stock", "items have zero stock"),
    "exceeds_stock": ("item is ordered beyond stock on hand", "items are ordered beyond stock on hand"),
    "non_positive_quantity": ("line has a zero or negative quantity", "lines have a zero or negative quantity"),
    "missing_quantity": ("line has no quantity", "lines have no quantity"),
    "line_total_exceeds_price": ("line is billed above quantity × price", "lines are billed above quantity × price"),
    "negative_total": "the total is negative",
    "missing_vendor": "no vendor is named",
    "missing_total": "no total is stated",
    "no_line_items": "there are no line items",
    "total_exceeds_line_items": "the total is more than the line items add up to",
    "total_unverifiable": "the total can't be verified",
    "invoice_number_reused": "this invoice number was already billed with different contents",
}
SEVERITY_KIND = {"must_reject": "block", "needs_scrutiny": "warn", "info": "info"}
SKIPPED_AGENTS = "Skipped: a hard rule already decides this invoice, so the agents weren't needed."
# A running step older than this gets a calm "Grok is slow" line (the client retries on its own).
SLOW_STEP_SECONDS = 30
SLOW_STEP_HINT = "Grok is taking longer than usual. It retries automatically; this can take up to a few minutes."


@dataclass
class Line:
    """One line under a step. kind sets its icon and style: text, ok, warn, block, info, agent, round or meta."""
    kind: str
    text: str


@dataclass
class Step:
    """One numbered step of the story; state is pending, running, done, warn (done with warnings), failed or skipped."""
    number: int
    title: str
    state: str
    lines: list[Line] = field(default_factory=list)


# ---------- formatting ----------

def nice_time(iso: str | None) -> str:
    """Format a stored ISO timestamp as "17 Nov 2026, 14:32 UTC" (times are stored in UTC)."""
    if not iso:
        return ""
    t = datetime.fromisoformat(iso)
    return f"{t.day} {t.strftime('%b %Y, %H:%M')} UTC"


def _justification(state: dict) -> str | None:
    """The reviewer's written justification for approving over a hard rule, if the run has one."""
    human = state.get("human_decision")
    if human is None:
        return None
    text = human.get("justification") if isinstance(human, dict) else getattr(human, "justification", None)
    return (text or "").strip() or None


def display_name(run_id: str, source_path: str | None) -> str:
    """The file's name as the user uploaded it (without the run-id prefix uploads are saved with)."""
    return Path(source_path or "").name.removeprefix(f"{run_id}_")


def field_words(path: str) -> str:
    """A field path in plain words: "due_date" -> "due date", "items[1].unit_price" -> "line 2 unit price"."""
    m = re.fullmatch(r"items\[(\d+)\](?:\.(\w+))?", path)
    if m:
        line = f"line {int(m.group(1)) + 1}"
        return f"{line} {FIELD_WORDS.get(m.group(2), m.group(2))}" if m.group(2) else line
    return FIELD_WORDS.get(path, path.replace("_", " "))


def plain_message(message: str) -> str:
    """A validation message in reviewer words: field paths and snake_case names spelled out, first letter capital."""
    text = re.sub(r"items\[\d+\](?:\.\w+)?", lambda m: field_words(m.group(0)), message)
    text = text.replace("qty x unit_price", "quantity × unit price")
    text = re.sub(r"\b(line_total|unit_price|due_date|invoice_number)\b", lambda m: FIELD_WORDS[m.group(1)], text)
    return text[:1].upper() + text[1:]


def short(text: str, sentences: int = 2, width: int = 320) -> str:
    """The first one or two sentences of an agent's reasoning on one line, cut to width."""
    line = " ".join((text or "").split())
    parts = re.split(r"(?<=[.!?])\s+", line)
    out = " ".join(parts[:sentences])
    return out if len(out) <= width else out[: width - 1].rstrip() + "…"


def blocking_summary(flags: list[Flag]) -> str | None:
    """One plain phrase for all must_reject flags (e.g. "2 items aren't in inventory"), or None."""
    counts: dict[str, int] = {}
    others: list[str] = []
    for f in flags:
        if f.severity != "must_reject":
            continue
        if f.code in _BLOCKING_PHRASES:
            counts[f.code] = counts.get(f.code, 0) + 1
        else:
            others.append(plain_message(f.message))
    parts = []
    for code, n in counts.items():
        phrase = _BLOCKING_PHRASES[code]
        parts.append(f"{n} {phrase[0] if n == 1 else phrase[1]}" if isinstance(phrase, tuple) else phrase)
    parts += others
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _money_differences(flags: list[Flag], currency: str) -> str:
    """Why money differences send an invoice to a person, e.g. "WidgetA's price differs from Acme's records"."""
    parts = []
    for f in flags:
        if f.code == "price_mismatch":
            # The message starts with the SKU ("WidgetA charged $300.00, ...").
            parts.append(f"{f.message.split(' charged ', 1)[0]}'s price differs from Acme's records")
        elif f.code == "arithmetic_mismatch":
            parts.append("the numbers don't add up")
        elif f.code == "amount_not_in_source":
            parts.append("an amount isn't printed on the invoice")
        elif f.code == "non_usd_currency":
            parts.append(f"it's billed in {currency}, so prices can't be compared")
    parts = list(dict.fromkeys(parts)) or ["prices, totals or currency differ from Acme's records"]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _decision_word(decision: str | None) -> str:
    """APPROVE -> "approve", REJECT -> "reject", for sentences like "agrees: reject"."""
    return {"APPROVE": "approve", "REJECT": "reject"}.get(decision or "", "decide")


def _recommends(decision: str) -> str:
    """The analyst's recommendation as a phrase."""
    return "Recommends approving" if decision == "APPROVE" else "Recommends rejecting"


def _vp_lines(r: Round) -> list[Line]:
    """The VP's verdict on one round: agrees (with its decision) or sends it back (with its feedback)."""
    rev = r.review
    if rev is None:
        return []
    if rev.verdict == "ACCEPT":
        return [Line("agent", f"Agrees: {_decision_word(rev.final_decision)}"), Line("text", short(rev.reasoning))]
    return [Line("agent", "Sends it back: " + short(rev.feedback or rev.reasoning, 2))]


def _lookups(state: dict) -> list[Line]:
    """Tool lookups the agent working right now has made (live only), e.g. "Looked up WidgetA stock and price"."""
    return [Line("info", text) for text in state.get("live_lookups") or []]


# ---------- the builder ----------

def build_story(state: dict, stage: str | None, live: bool, original_name: str | None = None,
                decided_at: str | None = None, now: datetime | None = None) -> list[Step]:
    """The 7 steps of an invoice's story from its (hydrated) state.
    stage: the last top-level graph node that finished; live: the run is still PROCESSING (unfinished steps show as
    running or pending); original_name: the file this one duplicates; decided_at: when the run was last saved (ISO);
    now: the current time (UTC), to say when a running step is taking unusually long."""
    if live and stage in NOT_STARTED:
        # A re-run's row still holds the previous result until the first node finishes: show none of it.
        state = {}
    final = None if live else state.get("final")
    inv, flags = state.get("invoice"), state.get("flags") or []
    approval: ApprovalOutcome | None = state.get("approval")
    rounds: list[Round] = approval.rounds if approval else list(state.get("live_rounds") or [])
    currency = inv.currency if inv else "USD"
    raw = state.get("raw_text")
    validated = "fingerprint" in state and state.get("fingerprint") is not None
    duplicate = bool(state.get("duplicate_of"))
    errored = final is not None and final.status == "ERROR"
    blocked = blocking_summary(flags) if validated and not duplicate else None
    # A hard rule decided it in validate, so the analyst and VP never ran (older runs may still have rounds).
    shortcut = blocked is not None and approval is None and not rounds

    steps: list[Step] = []

    # 1. Read the file.
    s1 = Step(1, "Read the file", "pending")
    if raw is not None:
        n = len(raw.splitlines())
        fmt = state.get("format") or ""
        pages = state.get("page_count")
        # A PDF is measured in pages; runs saved before page counts existed fall back to lines.
        what = f"{pages} page{'s' if pages != 1 else ''}" if fmt == "pdf" and pages else f"{n} lines"
        s1.state, s1.lines = "done", [Line("text", f"{FORMAT_LABELS.get(fmt, 'File')}, {what}")]
    steps.append(s1)

    # 2. Ingestion agent.
    s2 = Step(2, "Ingestion agent read it", "pending")
    if inv is not None:
        uncs = state.get("uncertainties") or []
        items = len(inv.items)
        due = f"due {nice_date(inv.due_date)}" if inv.due_date else "no due date found"
        s2.lines = [Line("text", f"{inv.vendor or 'Unknown vendor'}, {money(inv.total, currency)}"),
                    Line("text", f"{items} item{'s' if items != 1 else ''}, {due}")]
        if currency.upper() != "USD":
            s2.lines.append(Line("info", f"Amounts are in {currency.upper()} and were not converted."))
        if uncs:
            unsure = list(dict.fromkeys(field_words(u.field_path) for u in uncs))
            s2.lines.append(Line("warn", "Unsure about: " + ", ".join(unsure)))
        s2.state = "warn" if uncs else "done"
    steps.append(s2)

    # 3. Inventory and rules (and the duplicate check, which ends the story here).
    s3 = Step(3, "Checked against inventory and rules", "pending")
    if validated:
        if duplicate:
            name = original_name or "an earlier invoice"
            s3.state = "warn"
            s3.lines = [Line("info", f"This invoice was already processed (as {name}), "
                                     "so it was not processed or paid again.")]
        else:
            codes = {f.code for f in flags}
            if inv is not None and inv.items and not codes & _STOCK_CODES:
                s3.lines.append(Line("ok", "All items are in stock"))
            for sev in ("must_reject", "needs_scrutiny", "info"):
                s3.lines += [Line(SEVERITY_KIND[sev], plain_message(f.message)) for f in flags if f.severity == sev]
            if not flags:
                s3.lines = [Line("ok", "Everything checks out")]
            s3.state = "warn" if any(f.severity != "info" for f in flags) else "done"
    steps.append(s3)

    # 4. Analyst agent (round 1). 5. VP agent (round 1, then any further rounds, indented).
    s4 = Step(4, "Analyst agent", "pending")
    s5 = Step(5, "VP agent reviews", "pending")
    if shortcut:
        for s in (s4, s5):
            s.state, s.lines = "skipped", [Line("meta", SKIPPED_AGENTS)]
    elif rounds:
        first = rounds[0].recommendation
        s4.state = "done"
        s4.lines = [Line("agent", _recommends(first.decision)), Line("text", short(first.reasoning))]
        s5.lines = _vp_lines(rounds[0])
        for r in rounds[1:]:
            s5.lines += [Line("round", f"Round {r.number}"), Line("agent", "Analyst: " + _recommends(r.recommendation.decision).lower()),
                         Line("text", short(r.recommendation.reasoning)), *_vp_lines(r)]
        if approval is not None:
            s5.state = "done"
        elif rounds[-1].review is not None and rounds[-1].review.verdict == "SEND_BACK":
            # Live: the VP sent it back and the analyst is working on the next round.
            s5.lines += [Line("round", f"Round {len(rounds) + 1}"), Line("text", "The analyst is revising..."),
                         *_lookups(state)]
        else:
            s5.lines += _lookups(state)
    steps += [s4, s5]

    # 6. Guardrail: the code rules that sit above the agents.
    s6 = Step(6, "Guardrail", "pending")
    waiting = (final is not None and final.status == "NEEDS_HUMAN_REVIEW") or \
        (final is None and approval is not None and approval.decision == "NEEDS_HUMAN_REVIEW")
    if approval is not None or shortcut or (final is not None and final.decided_by == "guardrail"):
        if blocked:
            s6.state = "warn"
            # Say what actually happened: only a rejection is "rejected"; a person may still decide (or have decided).
            if final is not None and final.decided_by == "human" and final.status == "PAID":
                why = _justification(state)
                s6.lines = [Line("warn", f"A hard rule is broken ({blocked}), but a person overrode it" +
                                 (f" with this justification: {why}" if why else " with a written justification."))]
            elif final is not None and final.decided_by == "human":
                s6.lines = [Line("block", f"A hard rule is broken ({blocked}). A person reviewed it and rejected it.")]
            elif waiting:
                s6.lines = [Line("block", f"A hard rule is broken ({blocked}), so it's waiting for a person to decide.")]
            elif shortcut:
                s6.lines = [Line("block", f"A hard rule is broken ({blocked}), so it's rejected without the agents.")]
            else:
                s6.lines = [Line("block", f"A hard rule is broken ({blocked}), so it's rejected regardless of the agents.")]
        elif approval is not None and approval.decision == "NEEDS_HUMAN_REVIEW":
            s6.state = "warn"
            if approval.reasoning.startswith(HUMAN_REVIEW_REASON.split("{")[0]):
                why = _money_differences(flags, currency)
                s6.lines = [Line("warn", f"No hard rules broken, but {why}, so a person must decide.")]
            else:
                s6.lines = [Line("warn", f"No hard rules broken, but the agents couldn't agree after "
                                         f"{len(approval.rounds)} rounds, so a person must decide.")]
        elif approval is not None and approval.decision == "REJECT":
            s6.state, s6.lines = "done", [Line("ok", "No hard rules broken. The agents' rejection stands.")]
        else:
            s6.state, s6.lines = "done", [Line("ok", "All clear: no hard rules broken.")]
    steps.append(s6)

    # 7. Decision.
    s7 = Step(7, "Decision", "pending")
    if final is not None:
        s7.state, s7.lines = _decision(final, state, flags, approval, currency, decided_at)
        s7.title = f"Decision: {STATUS_LABELS[final.status]}"
    steps.append(s7)

    _set_progress(steps, live, stage, errored, duplicate, final, state, now)
    return steps


def _decision(final, state: dict, flags: list[Flag], approval, currency: str,
              decided_at: str | None) -> tuple[str, list[Line]]:
    """Step 7's state and lines for a finished run: paid, rejected, waiting for you, duplicate or error."""
    inv = state.get("invoice")
    when = f", {nice_time(decided_at)}" if decided_at else ""
    who = Line("meta", f"Decided by {DECIDED_BY_LABELS[final.decided_by]}{when}")
    note = [Line("text", f"Reviewer note: {final.human_note}")] if final.human_note else []
    if final.status == "PAID":
        amount = money(inv.total, currency) if inv else ""
        # rstrip: a vendor like "Widgets Inc." must not end the sentence with two periods.
        vendor = (inv.vendor if inv else None) or "the vendor"
        return "done", [Line("ok", f"Paid {amount} to {vendor.rstrip('.')}."), *note, who]
    if final.status == "REJECTED":
        if final.decided_by == "guardrail":
            reason = f"Rejected: {blocking_summary(flags) or 'a hard rule is broken'}."
        elif final.decided_by == "human":
            reason = "Rejected by a reviewer."
        else:
            reason = "Rejected: " + short(final.reasoning, 1)
        return "failed", [Line("block", reason), *note, who]
    if final.status == "NEEDS_HUMAN_REVIEW":
        rec = approval.rounds[-1] if approval and approval.rounds else None
        agents = (rec.review.final_decision if rec and rec.review and rec.review.final_decision
                  else rec.recommendation.decision if rec else None)
        lines = [Line("warn", "Waiting for you: nothing has been paid.")]
        if agents:
            lines.append(Line("text", f"The agents recommended {'approving' if agents == 'APPROVE' else 'rejecting'}."))
        return "warn", lines
    if final.status == "DUPLICATE":
        return "done", [Line("info", "Duplicate: not processed or paid again."), who]
    # ERROR: the failing step itself carries the reason; this one says what it means for the money.
    return "failed", [Line("block", "Stopped: this invoice couldn't be processed, so nothing was paid."), who]


def _slow(state: dict, now: datetime | None) -> bool:
    """True when the running step started (per the saved progress) more than SLOW_STEP_SECONDS ago."""
    started = state.get("step_started_at")
    if not started or now is None:
        return False
    return (now - datetime.fromisoformat(started)).total_seconds() > SLOW_STEP_SECONDS


def _set_progress(steps: list[Step], live: bool, stage: str | None, errored: bool, duplicate: bool,
                  final, state: dict, now: datetime | None = None) -> None:
    """Mark the steps not yet reached: the next one running (live), failed (error) or skipped (finished early)."""
    if duplicate:
        # A duplicate's story ends at step 3: the agents and the guardrail never see it.
        for s in steps[3:6]:
            s.state = "skipped"
    open_steps = [s for s in steps if s.state == "pending"]
    if live:
        if open_steps and stage != QUEUED:
            nxt = open_steps[0]
            nxt.state = "running"
            nxt.title = {1: "Reading the file...", 2: "Ingestion agent is reading it...",
                         3: "Checking against inventory and rules...", 4: "Analyst agent is investigating...",
                         5: "VP agent is reviewing...", 6: "Guardrail is checking...",
                         7: "Deciding..."}[nxt.number]
            if nxt.number == 4:
                nxt.lines = _lookups(state)
            if _slow(state, now):
                nxt.lines.append(Line("meta", SLOW_STEP_HINT))
        return
    if errored:
        reason = Line("block", plain_message(" ".join(final.reasoning.split())))
        failed = next((s for s in open_steps if s.number != 7), None)
        if failed is not None:
            failed.state = "failed"
            failed.lines = [reason]
        else:
            # Every earlier step finished (e.g. the payment call failed): the reason goes on the decision.
            steps[6].lines.insert(1, reason)
    for s in steps:
        if s.state == "pending":
            s.state = "skipped"


def story_json(steps: list[Step]) -> list[dict]:
    """The steps as plain dicts for the /status endpoint and live.js."""
    return [asdict(s) for s in steps]


def lookup_text(tool: str, args: dict) -> str | None:
    """A tool call by the analyst or VP as a plain sentence, e.g. "Looked up WidgetA stock and price"; None to skip."""
    if tool == "get_inventory":
        return f"Looked up {args.get('item', 'an item')} stock and price"
    if tool == "get_invoice_history":
        who = args.get("vendor") or args.get("invoice_number")
        return f"Checked past invoices for {who}" if who else "Checked past invoices"
    if tool == "get_approval_policy":
        return "Read the approval policy"
    if tool == "calculate":
        return f"Calculated {args.get('expression', '')}".strip()
    # The structured answer itself also arrives as a tool call: not a lookup.
    return None
