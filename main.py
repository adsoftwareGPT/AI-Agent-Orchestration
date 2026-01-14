import os
import sys
import time
from typing import Tuple

from orchestrator.core import PersistentOrchestrator
from orchestrator.types import TaskPacket
from orchestrator.llm import LLMClient

def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: python main.py \"<objective>\"\n")
        raise SystemExit(2)

    objective = sys.argv[1]

    # Optional allowlist via env FILES_ALLOWED="a.py,b.py"
    allow = os.environ.get("FILES_ALLOWED", "").strip()
    files_allowed: Tuple[str, ...] = tuple([p.strip() for p in allow.split(",") if p.strip()])

    # Task ID for persistence
    task_id = os.environ.get("TASK_ID", f"task_{int(time.time())}")
    
    # Check if we're resuming
    resume = os.environ.get("RESUME", "").lower() in ("1", "true", "yes")
    
    if resume:
        # Find the latest task directory
        base_dir = os.path.abspath("sample_project")
        if not os.path.exists(base_dir):
            os.makedirs(base_dir, exist_ok=True)
        
        # Look for existing task directories
        task_dirs = []
        for item in os.listdir(base_dir):
            item_path = os.path.join(base_dir, item)
            if os.path.isdir(item_path) and item.startswith("task_"):
                task_dirs.append(item_path)
        
        if not task_dirs:
            print("[DEBUG] No task directories found to resume", file=sys.stderr)
            resume = False
        else:
            # Use the most recent task directory
            task_dirs.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            workspace_dir = task_dirs[0]
            print(f"[DEBUG] Resuming from {workspace_dir}", file=sys.stderr)
    else:
        # Create new workspace
        base_dir = os.path.abspath("sample_project")
        if not os.path.exists(base_dir):
            os.makedirs(base_dir, exist_ok=True)
        
        workspace_dir = os.path.join(base_dir, task_id)
        os.makedirs(workspace_dir, exist_ok=True)

    # Setup logs
    logs_dir = os.path.abspath("logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, f"trace_{int(time.time())}.log")

    packet = TaskPacket(
        objective=objective,
        workspace_dir=workspace_dir,
        files_allowed=files_allowed,
        task_id=task_id
    )

    llm = LLMClient(log_path=log_file)
    
    if resume:
        # Try to resume existing workflow
        orchestrator = PersistentOrchestrator(llm, workspace_dir, task_id)
        code = orchestrator.run()
    else:
        # Start new workflow
        orchestrator = PersistentOrchestrator(llm, workspace_dir, task_id)
        code = orchestrator.run(packet)
    
    raise SystemExit(code)


if __name__ == "__main__":
    main()
