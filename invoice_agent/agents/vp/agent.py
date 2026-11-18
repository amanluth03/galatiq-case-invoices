"""Builds the Grok VP agent (spot-check tools + structured VPReview) used as the VPFn of the approval loop."""
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import BaseTool

from invoice_agent.agents.approval.loop import ApprovalContext, VPFn
from invoice_agent.agents.approval.render import render_context
from invoice_agent.agents.approval.tools import VP_TOOLS
from invoice_agent.agents.vp.prompt import VP_PROMPT
from invoice_agent.models import Recommendation, Round, VPReview


def make_vp(model, tools: dict[str, BaseTool]) -> VPFn:
    """Wrap a tool-calling agent as a VPFn; tools is make_tools(conn), of which it takes only the VP's set (no history)."""
    agent = create_agent(
        model, tools=[tools[name] for name in VP_TOOLS],
        response_format=ToolStrategy(VPReview), system_prompt=VP_PROMPT,
    )

    def vp(ctx: ApprovalContext, recommendation: Recommendation, rounds: list[Round]) -> VPReview:
        """Brief the agent on the invoice, earlier rounds and the draft to review, and return its VPReview."""
        text = render_context(ctx, rounds, recommendation=recommendation)
        result = agent.invoke({"messages": [{"role": "user", "content": text}]})
        return result["structured_response"]

    return vp
