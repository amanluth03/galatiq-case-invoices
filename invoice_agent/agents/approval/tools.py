"""LangChain tools for the analyst and VP agents (inventory, history, policy, calculator); used in the approval stage."""
import ast
import json
import operator
import sqlite3
import threading
from decimal import Decimal

from langchain_core.tools import BaseTool, tool

from invoice_agent.database import db
from invoice_agent.agents.approval.policy import APPROVAL_POLICY
from invoice_agent.utils.normalize import normalize_invoice_number

ANALYST_TOOLS = ["get_inventory", "get_invoice_history", "get_approval_policy", "calculate"]
# VP gets no history tool on purpose: missing evidence goes back to the analyst.
VP_TOOLS = ["get_approval_policy", "get_inventory"]

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_UNARYOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node: ast.AST) -> Decimal:
    """Evaluate an arithmetic AST node with Decimal; anything but numbers and + - * / raises ValueError."""
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Decimal(str(node.value))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_eval(node.operand))
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def make_tools(conn: sqlite3.Connection) -> dict[str, BaseTool]:
    """Build the agent tools as closures over the database connection."""
    # LangChain runs an agent's parallel tool calls on worker threads, and one sqlite3 connection used by two threads
    # at once mixes up rows (live: "conversion from NoneType to Decimal" on 1006). One query at a time per connection.
    db_lock = threading.Lock()

    @tool
    def get_inventory(item: str) -> str:
        """Look up an inventory item by SKU (e.g. WidgetA). Returns its stock on hand and reference unit price, or says it was not found."""
        with db_lock:
            row = db.get_inventory(conn, item)
        if row is None:
            return f"{item}: not found in inventory"
        return json.dumps(row, default=str)

    @tool
    def get_invoice_history(vendor: str | None = None, invoice_number: str | None = None) -> str:
        """Find previously processed invoices, filtered by vendor name (case-insensitive) and/or exact invoice number. Returns number, vendor, amount, status and date for each."""
        wanted = normalize_invoice_number(invoice_number) if invoice_number else None
        out = []
        with db_lock:
            runs = db.list_runs(conn)
        for r in runs:
            if vendor and (r["vendor"] or "").lower() != vendor.strip().lower():
                continue
            if wanted and normalize_invoice_number(r["invoice_number"]) != wanted:
                continue
            out.append({k: r[k] for k in ("invoice_number", "vendor", "amount", "status", "created_at")})
        return json.dumps(out, default=str) if out else "no prior invoices found"

    @tool
    def get_approval_policy() -> str:
        """Return the approval policy: flag codes and what they mean for the decision, plus the over-$10K checklist."""
        return APPROVAL_POLICY

    @tool
    def calculate(expression: str) -> str:
        """Evaluate an arithmetic expression using + - * / and parentheses with decimal numbers, e.g. "15*250 + 5*240". Returns the result, or an error message."""
        try:
            result = _eval(ast.parse(expression.strip(), mode="eval"))
            # A float literal like 1e999 becomes Infinity; never hand the agent a non-number as a result.
            if not result.is_finite():
                return "error: result is not a finite number"
            return format(result.normalize(), "f")
        # ArithmeticError covers division by zero and Decimal overflow; RecursionError a deeply nested expression.
        except (ValueError, SyntaxError, ArithmeticError, RecursionError) as e:
            return f"error: {e}"

    return {t.name: t for t in (get_inventory, get_invoice_history, get_approval_policy, calculate)}
