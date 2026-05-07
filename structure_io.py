from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field
from typing import List
import os

load_dotenv()

class MovieReview(BaseModel):
    title: str
    hero: str
    rating: int = Field(ge=1, le=10, description="1=worst, 10=masterpiece")
    pros: List[str]
    cons: List[str]
    ua: str = Field(description="U, A, U/A rating for the movie")
    year: int = Field(ge=1888, le=2024, description="Year the movie was released")

api_key = os.getenv("CLAUDE_API_KEY")
if not api_key:
    raise RuntimeError("CLAUDE_API_KEY is missing. Add it to your .env file.")

llm = ChatAnthropic(model="claude-sonnet-4-6", api_key=api_key)
structured = llm.with_structured_output(MovieReview)

result = structured.invoke("Review the movie 'ranga sslc'")
print(result.title, "-", result.rating, "/10")
print("Pros:", result.pros)
print("Cons:", result.cons)
print("title:", result.title)
print("Hero:", result.hero)
print("Cert:", result.ua)
print("Year:", result.year)
#print("Type:", type(result))  # <class '__main__.MovieReview'>
