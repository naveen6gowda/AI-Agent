import os
from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_anthropic import ChatAnthropic


load_dotenv()
api_key = os.getenv("CLAUDE_API_KEY")

llm = ChatAnthropic(model="claude-sonnet-4-6", api_key=api_key)

prompt = ChatPromptTemplate.from_messages([
    ("system", "Get the population of indian states in crore {year}"), ("human", "{question}")
])

chain = prompt | llm | StrOutputParser()

answer = chain.invoke({"year": "2025", "question": "Highest population state in india?"})
print(answer)   