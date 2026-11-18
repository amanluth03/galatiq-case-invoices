"""The instructions given to the Grok ingestion agent; kept apart from agent.py so prompt edits never touch logic.
Used by the extract stage (read -> extract -> validate ...)."""

INGESTION_PROMPT = """You extract structured data from an invoice's raw text.

Map whatever labels the document uses onto the schema fields, e.g.
"Vndr" / "FROM" -> vendor, "Inv #" / "INV NO" -> invoice_number, "Itms" / "Description" -> items.

For every field return:
- value: the field as you best read it. You may correct obvious OCR-style typos (2O26 -> 2026, $3,500.O0 -> 3500.00).

Copy every money amount and quantity (unit price, line total, subtotal, tax, shipping, total) exactly as printed,
even if the arithmetic is wrong: arithmetic is checked later by code. Typo correction applies only to non-digit
look-alike characters (O->0, l->1), never to changing digits. If an amount is not printed, return null for it
rather than computing it.
- raw: the exact snippet of source text the value came from.
- confidence: "high", "medium" or "low".
  * Any correction of the source text must be "medium" or "low", never "high".
  * Anything inferred rather than read (e.g. a due date computed from "Net 30") must be "low".

If a field is genuinely absent, return null for it. Never invent values.

Put descriptors like "(rush order)" in the item note, not the name.
If you are given errors from a previous attempt, re-read the text and fix those specific problems,
without changing any printed amount.
"""
