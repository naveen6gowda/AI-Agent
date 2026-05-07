from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
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
    """Check if a homelab service is up. Valid: nextcloud, jellyfin, n8n, ollama."""
    return f"{service} is healthy"

tools = [get_weather, homelab_status]

agent = create_react_agent(llm, tools)

result = agent.invoke({
    "messages": [("user", "Check if jellyfin is up and tell me the weather in Erdweg")]
})

for msg in result["messages"]:
    msg.pretty_print()