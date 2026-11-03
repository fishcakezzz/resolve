"""Bounded evidence inspection shared by the two specialized investigators."""

from ..policy import required_evidence
from ..llm import LLMConfig, select_investigation_field
from .contracts import AgentState, EvidenceSnapshotTool


def investigate(state: AgentState, role: str, allowed_types: set[str]) -> dict:
    task = next((task for task in state["tasks"] if task["role"] == role), None)
    if task is None:
        return {"role": role, "status": "SKIPPED", "task_id": None, "findings": [], "steps": 0}
    allowed_fields = set(task["allowed_fields"])
    tool = EvidenceSnapshotTool(allowed_fields, state["facts"], state["evidence_refs"])
    relevant = [issue for issue in state["issues"] if issue["issue_type"] in allowed_types]
    remaining = sorted(set().union(*(required_evidence(issue["issue_type"]) & allowed_fields for issue in relevant)))
    observations = []
    observed = {}
    config = LLMConfig.from_env()
    while remaining and len(observations) < task["max_steps"]:
        field = select_investigation_field(role, task["issue_keys"], remaining, observations, config) if config else remaining[0]
        observation = tool.read(field)
        observed[field] = observation
        observations.append({"field": field, "status": observation["status"]})
        remaining.remove(field)
    findings = []
    for issue in relevant:
        fields = sorted(required_evidence(issue["issue_type"]) & allowed_fields)
        evidence_refs = []
        missing_fields = []
        for field in fields:
            observation = observed.get(field, {"status": "MISSING"})
            if observation["status"] == "AVAILABLE":
                if observation["evidence_ref"]:
                    evidence_refs.append(observation["evidence_ref"])
            else:
                missing_fields.append(field)
        findings.append({
            "issue_key": issue["key"],
            "status": "NEEDS_INFO" if missing_fields else "EVIDENCE_READY",
            "missing_fields": missing_fields,
            "evidence_refs": evidence_refs,
        })
    return {"role": role, "task_id": task["task_id"], "status": "NEEDS_INFO" if any(f["missing_fields"] for f in findings) else "COMPLETED", "findings": findings, "steps": len(observations)}
