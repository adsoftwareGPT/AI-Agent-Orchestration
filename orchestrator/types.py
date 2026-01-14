import time
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

class State(Enum):
    SPEC = auto()
    SPEC_REVIEW = auto()
    SPEC_REPAIR = auto()
    PLAN = auto()
    PATCH = auto()
    PATCH_REVIEW = auto()
    APPLY = auto()
    TEST = auto()
    REPAIR_PATCH = auto()
    DONE = auto()
    FAILED = auto()

@dataclass(frozen=True)
class TaskPacket:
    objective: str
    workspace_dir: str = "."
    # Optional allowlist. If empty, any file under workspace is allowed.
    files_allowed: Tuple[str, ...] = ()
    task_id: str = field(default_factory=lambda: f"task_{int(time.time())}")

@dataclass
class RunContext:
    packet: TaskPacket
    frozen_spec: Optional[Dict[str, Any]] = None
    plan: Optional[Dict[str, Any]] = None
    patches: List[Dict[str, Any]] = field(default_factory=list)
    test_reports: List[Dict[str, Any]] = field(default_factory=list)
    
    # State machine transient data
    spec_review: Optional[Dict[str, Any]] = None
    patch_review: Optional[Dict[str, Any]] = None
    latest_research_report: Optional[str] = None
    
    # Add state tracking
    current_state: State = State.SPEC
    iteration_count: int = 0
