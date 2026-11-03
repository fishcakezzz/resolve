"""Coordinator -> parallel investigators -> resolution planner."""

from langgraph.graph import END, START, StateGraph

from .contracts import AgentState
from .coordinator import dispatch
from .fulfillment import run as fulfillment_run
from .payment import run as payment_run
from .resolution import run as resolution_run


builder = StateGraph(AgentState)
builder.add_node("case_coordinator", dispatch)
builder.add_node("fulfillment_investigator", fulfillment_run)
builder.add_node("payment_investigator", payment_run)
builder.add_node("resolution_planner", resolution_run)
builder.add_edge(START, "case_coordinator")
builder.add_edge("case_coordinator", "fulfillment_investigator")
builder.add_edge("case_coordinator", "payment_investigator")
builder.add_edge(["fulfillment_investigator", "payment_investigator"], "resolution_planner")
builder.add_edge("resolution_planner", END)
analysis_graph = builder.compile()
