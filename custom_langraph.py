from typing import TypedDict, Annotated
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from dotenv import load_dotenv
import os

load_dotenv()
api_key = os.getenv("CLAUDE_API_KEY")

llm = ChatAnthropic(model="claude-sonnet-4-6", api_key=api_key)

@tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"Weather in {city}: 18°C, partly cloudy"

@tool
def homelab_status(service: str) -> str:
    """Check if a homelab service is up."""
    return f"{service} is healthy"

tools = [get_weather, homelab_status]
llm_with_tools = llm.bind_tools(tools)

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]

def call_model(state: AgentState):
    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}

graph = StateGraph(AgentState)
graph.add_node("agent", call_model)
graph.add_node("tools", ToolNode(tools))

graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", tools_condition)  # routes to "tools" or END
graph.add_edge("tools", "agent")

app = graph.compile()

# See the structure
print(app.get_graph().draw_ascii())

# Run it
result = app.invoke({
    "messages": [("user", "Weather in Munich and is n8n up?")]
})
for msg in result["messages"]:
    msg.pretty_print()