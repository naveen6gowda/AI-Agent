from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from dotenv import load_dotenv
import os

load_dotenv()

api_key = os.getenv("CLAUDE_API_KEY")

llm = ChatAnthropic(model="claude-sonnet-4-6", api_key=api_key)

prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a {role}. Be concise — max 3 sentences."),
    ("human", "{question}")
])

chain = prompt | llm | StrOutputParser()

answer = chain.invoke({
    "role": "Linux sysadmin",
    "question": "How do I find files larger than 100MB?"
})
print(answer)