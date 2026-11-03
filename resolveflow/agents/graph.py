"""Coordinator -> parallel investigators -> resolution planner."""

from contextlib import contextmanager

from sqlalchemy.engine import make_url

from langgraph.graph import END, START, StateGraph

from .contracts import AgentState
from .coordinator import dispatch, review
from .fulfillment import run as fulfillment_run
from .payment import run as payment_run
from .resolution import run as resolution_run


builder = StateGraph(AgentState)
builder.add_node("case_coordinator", dispatch)
builder.add_node("fulfillment_investigator", fulfillment_run)
builder.add_node("payment_investigator", payment_run)
builder.add_node("coordinator_review", review)
builder.add_node("resolution_planner", resolution_run)
builder.add_edge(START, "case_coordinator")
builder.add_edge("case_coordinator", "fulfillment_investigator")
builder.add_edge("case_coordinator", "payment_investigator")
builder.add_edge(["fulfillment_investigator", "payment_investigator"], "coordinator_review")
builder.add_edge("coordinator_review", "resolution_planner")
builder.add_edge("resolution_planner", END)
analysis_graph = builder.compile()


@contextmanager
def durable_graph():
    """Persist node checkpoints independently of the Case business transaction."""
    from ..db import DATABASE_URL

    url = make_url(DATABASE_URL)
    if url.drivername.startswith("sqlite"):
        from langgraph.checkpoint.sqlite import SqliteSaver

        with SqliteSaver.from_conn_string(f"{url.database}.checkpoints.db") as saver:
            yield builder.compile(checkpointer=saver)
    elif url.drivername.startswith("postgresql"):
        from langgraph.checkpoint.postgres import PostgresSaver

        dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            yield builder.compile(checkpointer=saver)
    else:
        raise RuntimeError("不支持的 LangGraph 检查点数据库")
