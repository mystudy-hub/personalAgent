"""Build the fixed CSV plan from validated Main template parameters."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    template: Literal["csv_blank_line"]
    source_file: Literal["csv_import.py"]
    test: Literal["unittest"]


def build_plan(project_id: str, parameters: PlanParameters) -> dict:
    if parameters.project_id != project_id:
        raise ValueError("Template parameters must reference the task's registered project")
    return {
        "template": parameters.template,
        "project_id": project_id,
        "nodes": [
            {
                "id": "research", "role": "Research", "depends_on": [],
                "input": parameters.source_file, "artifact": "findings",
                "acceptance": "Locate the CSV parsing code and confirm the blank-line failure.",
            },
            {
                "id": "coding", "role": "Coding", "depends_on": ["research"],
                "input": "findings", "artifact": "candidate",
                "acceptance": f"Modify only {parameters.source_file} and pass the registered {parameters.test} regression.",
            },
            {
                "id": "review", "role": "Review", "depends_on": ["coding"],
                "input": "candidate", "artifact": "review",
                "acceptance": "Check the candidate diff, failing baseline and passing regression.",
            },
        ],
    }


def parse_model_plan(project_id: str, answer: str) -> dict:
    text = answer.removeprefix("```json").removesuffix("```").strip()
    return build_plan(project_id, PlanParameters.model_validate_json(text))
