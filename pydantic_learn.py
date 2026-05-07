from pydantic import BaseModel, Field
from typing import Literal

class TaskItem(BaseModel):
    """A single task in a todo list."""
    title: str = Field(description="Short task name")
    priority: Literal["low", "medium", "high"]
    estimated_minutes: int = Field(gt=0, le=480)

task = TaskItem(title="Review PR", priority="high", estimated_minutes=30)
print(task.model_dump_json(indent=2))

try:
    bad = TaskItem(title="X", priority="urgent", estimated_minutes=-5)
except Exception as e:
    print("Validation caught it:", e)