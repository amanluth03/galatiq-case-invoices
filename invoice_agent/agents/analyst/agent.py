"""Builds the Grok analyst agent (tools + structured Recommendation) used as the AnalystFn of the approval loop."""
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import BaseTool

from invoice_agent.agents.analyst.prompt import ANALYST_PROMPT
from invoice_agent.agents.approval.loop import AnalystFn, ApprovalContext
from invoice_agent.agents.approval.render import render_context
from invoice_agent.agents.approval.tools import ANALYST_TOOLS
from invoice_agent.models import Recommendation, Round


def make_analyst(model, tools: dict[str, BaseTool]) -> AnalystFn:
    """Wrap a tool-calling agent as an AnalystFn; tools is make_tools(conn), of which it takes the analyst's set."""
    # ToolStrategy: the answer comes back as a tool call, so Grok can use real tools and structured output together.
    agent = create_agent(
        model, tools=[tools[name] for name in ANALYST_TOOLS],
        response_format=ToolStrategy(Recommendation), system_prompt=ANALYST_PROMPT,
    )

    def analyst(ctx: ApprovalContext, rounds: list[Round]) -> Recommendation:
        """Brief the agent on the invoice and earlier rounds, and return its Recommendation."""
        result = agent.invoke({"messages": [{"role": "user", "content": render_context(ctx, rounds)}]})
        return result["structured_response"]

    return analyst
