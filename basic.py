import os

from openai import OpenAI

client = OpenAI(base_url="http://192.168.178.75:8383/v1",  # Your llama.cpp server URL
    api_key="c34252bf3982850fc5a93c093bb7adb2"  # llama.cpp doesn't require authentication
)


completion = client.chat.completions.create(
    model="active",
    messages=[
        {"role": "system", "content": "You're a helpful assistant."},
        {
            "role": "user",
            "content": "Write a limerick about the Python programming language.",
        },
    ],
)

response = completion.choices[0].message.content
print(response)