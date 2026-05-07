from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from dotenv import load_dotenv
import os

load_dotenv()

api_key = os.getenv("CLAUDE_API_KEY")

llm = ChatAnthropic(model="claude-sonnet-4-6", api_key=api_key)

@tool
def get_weather(city: str) -> str:
    """Get current weather for a city. Use for any weather question."""
    return f"Weather in {city}: 18°C, partly cloudy"

@tool
def homelab_status(service: str) -> str:
    """Check if a homelab service is up. Valid services: nextcloud, jellyfin, n8n, ollama."""
    return f"{service} is healthy and responding"

@tool
def add_numbers(a: float, b: float) -> float:
    """Add two numbers together."""
    return a + b

tools = [get_weather, homelab_status, add_numbers]

llm_with_tools = llm.bind_tools(tools)

response = llm_with_tools.invoke("What's the weather in Munich and is jellyfin up?")
print("Content:", response.content)
print("Tool calls:", response.tool_calls)
