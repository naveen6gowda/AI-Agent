import json
from dotenv import load_dotenv
import os

import requests
from anthropic import Anthropic

load_dotenv()

client = Anthropic(
    api_key=os.getenv("CLAUDE_API_KEY")
)

"""
docs: https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/overview
"""

# --------------------------------------------------------------
# Define the tool (function) that we want to call
# --------------------------------------------------------------


def get_weather(latitude, longitude):
    """This is a publically available API that returns the weather for a given location."""
    response = requests.get(
        f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,wind_speed_10m&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m"
    )
    data = response.json()
    return data["current"]


# --------------------------------------------------------------
# Step 1: Call model with get_weather tool defined
# --------------------------------------------------------------

tools = [
    {
        "name": "get_weather",
        "description": "Get current temperature for provided coordinates in celsius.",
        "input_schema": {
            "type": "object",
            "properties": {
                "latitude": {"type": "number"},
                "longitude": {"type": "number"},
            },
            "required": ["latitude", "longitude"],
        },
    }
]

system_prompt = "You are a helpful weather assistant."

messages = [
    {"role": "user", "content": "What's the weather like in Paris today?"},
]

completion = client.messages.create(
    model="claude-3-5-sonnet-20241022",
    max_tokens=1024,
    system=system_prompt,
    messages=messages,
    tools=tools,
)

# --------------------------------------------------------------
# Step 2: Model decides to call function(s)
# --------------------------------------------------------------

# Display the response
print(completion)

# --------------------------------------------------------------
# Step 3: Execute get_weather function
# --------------------------------------------------------------


def call_function(name, args):
    if name == "get_weather":
        return get_weather(**args)
    raise ValueError(f"Unknown function: {name}")

# Process tool use blocks
for block in completion.content:
    if block.type == "tool_use":
        tool_call = block
        name = tool_call.name
        args = tool_call.input
        messages.append({"role": "assistant", "content": completion.content})
        
        result = call_function(name, args)
        messages.append(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": json.dumps(result)}]}
        )

# --------------------------------------------------------------
# Step 4: Supply result and call model again
# --------------------------------------------------------------


completion_2 = client.messages.create(
    model="claude-3-5-sonnet-20241022",
    max_tokens=1024,
    system=system_prompt,
    messages=messages,
    tools=tools,
)

# --------------------------------------------------------------
# Step 5: Check model response
# --------------------------------------------------------------

# Extract the text response
for block in completion_2.content:
    if hasattr(block, "text"):
        print(block.text)
