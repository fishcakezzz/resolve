from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


IssueType = Literal["MISSING_ITEM", "LATE_DELIVERY", "DUPLICATE_CHARGE"]


class IssueIn(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    issue_type: IssueType
    requested_amount_cents: int = Field(default=0, ge=0)
    sku: str | None = None
    depends_on: list[str] = Field(default_factory=list)


class FactIn(BaseModel):
    value: Any
    source_system: Literal["CRM", "OMS", "PAYMENT", "LOGISTICS", "KNOWLEDGE"]
    source_id: str = Field(min_length=1)


class CaseCreate(BaseModel):
    request_key: str = Field(min_length=1, max_length=120)
    customer_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    request_text: str | None = Field(default=None, max_length=5000)
    issues: list[IssueIn] = Field(min_length=1)
    demo_facts: dict[str, FactIn] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self):
        keys = [item.key for item in self.issues]
        if len(keys) != len(set(keys)):
            raise ValueError("Issue keys must be unique")
        dependencies = {item.key: item.depends_on for item in self.issues}
        for key, parents in dependencies.items():
            if key in parents or any(parent not in dependencies for parent in parents):
                raise ValueError("Invalid issue dependency")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str):
            if key in visiting:
                raise ValueError("Issue dependencies must be acyclic")
            if key in visited:
                return
            visiting.add(key)
            for parent in dependencies[key]:
                visit(parent)
            visiting.remove(key)
            visited.add(key)

        for key in keys:
            visit(key)
        return self


class CaseDraftIn(BaseModel):
    request_text: str = Field(min_length=1, max_length=5000)


class EvidenceUpdate(BaseModel):
    value: Any
    source_system: Literal["CRM", "OMS", "PAYMENT", "LOGISTICS", "KNOWLEDGE"]
    source_id: str = Field(min_length=1)


class ApprovalIn(BaseModel):
    approver_id: str = Field(min_length=1)
    decision: Literal["APPROVE", "REJECT", "MODIFY"]
    comment: str = ""
    amount_cents: int | None = Field(default=None, ge=0)


class ExecuteIn(BaseModel):
    expected_case_version: int = Field(ge=1)


class TakeoverIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    issue_keys: list[str] = Field(default_factory=list)
