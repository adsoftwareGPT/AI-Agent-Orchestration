import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .types import RunContext, State, TaskPacket

class StateManager:
    """Manages persistent state storage for the workflow"""
    
    def __init__(self, workspace_dir: str, task_id: str):
        self.workspace_dir = workspace_dir
        self.state_dir = os.path.join(workspace_dir, ".agent_state")
        self.task_id = task_id
        os.makedirs(self.state_dir, exist_ok=True)
        
        # Create a changelog directory for file history
        self.changelog_dir = os.path.join(self.state_dir, "changelogs")
        os.makedirs(self.changelog_dir, exist_ok=True)
    
    def save_context(self, ctx: RunContext) -> None:
        """Save the entire context to disk"""
        context_path = os.path.join(self.state_dir, "context.json")
        # Convert to serializable format
        context_dict = {
            "packet": {
                "objective": ctx.packet.objective,
                "workspace_dir": ctx.packet.workspace_dir,
                "files_allowed": list(ctx.packet.files_allowed),
                "task_id": ctx.packet.task_id
            },
            "frozen_spec": ctx.frozen_spec,
            "plan": ctx.plan,
            "patches": ctx.patches,
            "test_reports": ctx.test_reports,
            "spec_review": ctx.spec_review,
            "patch_review": ctx.patch_review,
            "current_state": ctx.current_state.name,
            "iteration_count": ctx.iteration_count
        }
        
        with open(context_path, 'w', encoding='utf-8') as f:
            json.dump(context_dict, f, indent=2, ensure_ascii=False)
        
        # Also save a backup with timestamp
        backup_path = os.path.join(self.state_dir, f"context_backup_{int(time.time())}.json")
        shutil.copy2(context_path, backup_path)
    
    def load_context(self) -> Optional[RunContext]:
        """Load context from disk if it exists"""
        context_path = os.path.join(self.state_dir, "context.json")
        if not os.path.exists(context_path):
            return None
        
        try:
            with open(context_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            packet = TaskPacket(
                objective=data["packet"]["objective"],
                workspace_dir=data["packet"]["workspace_dir"],
                files_allowed=tuple(data["packet"]["files_allowed"]),
                task_id=data["packet"]["task_id"]
            )
            
            ctx = RunContext(packet=packet)
            ctx.frozen_spec = data.get("frozen_spec")
            ctx.plan = data.get("plan")
            ctx.patches = data.get("patches", [])
            ctx.test_reports = data.get("test_reports", [])
            ctx.spec_review = data.get("spec_review")
            ctx.patch_review = data.get("patch_review")
            ctx.current_state = State[data.get("current_state", "SPEC")]
            ctx.iteration_count = data.get("iteration_count", 0)
            
            return ctx
        except Exception as e:
            print(f"[DEBUG] Failed to load context: {e}", file=sys.stderr)
            return None
    
    def save_artifact(self, name: str, artifact: Dict[str, Any]) -> None:
        """Save a specific artifact (spec, plan, patch, etc.)"""
        artifact_dir = os.path.join(self.state_dir, "artifacts")
        os.makedirs(artifact_dir, exist_ok=True)
        
        artifact_path = os.path.join(artifact_dir, f"{name}_{int(time.time())}.json")
        with open(artifact_path, 'w', encoding='utf-8') as f:
            json.dump(artifact, f, indent=2, ensure_ascii=False)
    
    def save_file_snapshot(self, file_path: str, content: str, agent: str) -> None:
        """Save a snapshot of a file before modification"""
        snapshot_dir = os.path.join(self.changelog_dir, agent)
        os.makedirs(snapshot_dir, exist_ok=True)
        
        snapshot_path = os.path.join(snapshot_dir, 
                                   f"{Path(file_path).name}_{int(time.time())}.snapshot")
        
        snapshot_data = {
            "timestamp": time.time(),
            "agent": agent,
            "file_path": file_path,
            "content": content,
            "task_id": self.task_id
        }
        
        with open(snapshot_path, 'w', encoding='utf-8') as f:
            json.dump(snapshot_data, f, indent=2, ensure_ascii=False)
    
    def get_file_history(self, file_path: str) -> List[Dict[str, Any]]:
        """Get the change history of a specific file"""
        history = []
        for agent_dir in os.listdir(self.changelog_dir):
            agent_path = os.path.join(self.changelog_dir, agent_dir)
            if not os.path.isdir(agent_path):
                continue
            
            for snapshot_file in os.listdir(agent_path):
                if snapshot_file.endswith('.snapshot'):
                    snapshot_path = os.path.join(agent_path, snapshot_file)
                    try:
                        with open(snapshot_path, 'r', encoding='utf-8') as f:
                            snapshot = json.load(f)
                            if snapshot.get("file_path") == file_path:
                                history.append(snapshot)
                    except:
                        continue
        
        # Sort by timestamp
        history.sort(key=lambda x: x.get("timestamp", 0))
        return history
