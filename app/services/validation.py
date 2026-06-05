from app.core.workflow import CandidateStage, DemandStatus, can_transition
from typing import Tuple

class WorkflowValidationError(Exception):
    pass

def validate_candidate_transition(current_stage: str, target_stage: str) -> None:
    """
    Backend validation rule to ensure candidate lifecycle bounds are respected.
    """
    if not can_transition(current_stage, target_stage):
        raise WorkflowValidationError(f"Invalid candidate transition from '{current_stage}' to '{target_stage}'")

def validate_demand_status_change(current_status: str, new_status: str) -> None:
    """
    Validation logic for Demand file status transitions.
    """
    allowed_transitions = {
        DemandStatus.ACTIVE.value: [DemandStatus.PROCESSING.value, DemandStatus.FILLED.value, DemandStatus.CANCELLED.value, DemandStatus.EXPIRED.value],
        DemandStatus.PROCESSING.value: [DemandStatus.FILLED.value, DemandStatus.ACTIVE.value, DemandStatus.CANCELLED.value],
        DemandStatus.FILLED.value: [DemandStatus.ACTIVE.value, DemandStatus.CANCELLED.value],
        DemandStatus.CANCELLED.value: [DemandStatus.ACTIVE.value],
        DemandStatus.EXPIRED.value: [DemandStatus.ACTIVE.value, DemandStatus.CANCELLED.value]
    }
    
    if new_status not in allowed_transitions.get(current_status, []):
        raise WorkflowValidationError(f"Invalid demand status transition from '{current_status}' to '{new_status}'")
