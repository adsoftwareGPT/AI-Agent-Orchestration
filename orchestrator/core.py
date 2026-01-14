import json
import re
import sys
from typing import Any, Dict, Optional

from .config import SYSTEM_ARCHITECT, SYSTEM_CODER_REPAIR, MAX_REPAIRS, MAX_SPEC_REPAIRS, DEFAULT_TEMPERATURE
from .types import RunContext, State, TaskPacket
from .state import StateManager
from .tools import PersistentTools
from .llm import LLMClient, assert_type
from .gates import GateRunner, extract_gates_from_spec
from .agents import (
    ArchitectAgent,
    CoderAgent,
    CriticAgent,
    PlannerAgent,
    ResearchAgent,
    TesterAgent,
)


class PersistentOrchestrator:
    def __init__(self, llm: LLMClient, workspace_dir: str, task_id: str) -> None:
        self.llm = llm
        self.workspace_dir = workspace_dir
        self.state_manager = StateManager(workspace_dir, task_id)
        
        # Load existing context if available
        self.ctx = self.state_manager.load_context()
        if self.ctx:
            print(f"[DEBUG] Loaded existing context from state {self.ctx.current_state}", file=sys.stderr)
            self.state = self.ctx.current_state
        else:
            self.state = State.SPEC
        
        self.max_repairs = MAX_REPAIRS
        self.repair_count = 0
        self.max_spec_repairs = MAX_SPEC_REPAIRS
        self.spec_repair_count = 0
    
    def save_state(self) -> None:
        """Save current state to disk"""
        if self.ctx:
            self.ctx.current_state = self.state
            self.state_manager.save_context(self.ctx)
    
    def create_agent_tools(self, agent_name: str) -> PersistentTools:
        """Create tools for a specific agent"""
        files_allowed = self.ctx.packet.files_allowed if self.ctx else ()
        return PersistentTools(self.workspace_dir, files_allowed, self.state_manager, agent_name)
    
    def run(self, packet: Optional[TaskPacket] = None) -> int:
        # Initialize context if not loaded
        if not self.ctx and packet:
            self.ctx = RunContext(packet=packet)
            self.state = State.SPEC
        elif not self.ctx:
            raise RuntimeError("No context loaded and no packet provided")
        
        while self.state not in [State.DONE, State.FAILED]:
            print(f"[DEBUG] Entering State: {self.state.name}", file=sys.stderr)
            self.ctx.iteration_count += 1
            
            try:
                if self.state == State.SPEC:
                    tools = self.create_agent_tools("architect")
                    spec = ArchitectAgent().run_with_tools(self.ctx, self.llm, tools)
                    assert_type(spec, "SPECIFICATION")
                    self.ctx.frozen_spec = spec
                    self.state_manager.save_artifact("spec", spec)
                    self.state = State.SPEC_REVIEW
                
                elif self.state == State.SPEC_REVIEW:
                    # AUTO-RESEARCH: Check URLs in spec before Review
                    if self.ctx.frozen_spec:
                         spec_str = json.dumps(self.ctx.frozen_spec)
                         urls = re.findall(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[^\s]*', spec_str)
                         if urls:
                             print(f"[DEBUG] Auto-Researching URLs in SPEC: {urls}", file=sys.stderr)
                             r_tools = self.create_agent_tools("researcher")
                             researcher = ResearchAgent()
                             report = "RESEARCHER REPORT (Verified URLs):\n"
                             for url in urls:
                                 url = url.strip('"\').,')
                                 report += researcher.verify_url(url, r_tools) + "\n"
                             
                             # Append this report to the context available to Critic
                             # We'll attach it to the 'spec_review' field temporarily or modify how Critic reads
                             self.ctx.latest_research_report = report

                    tools = self.create_agent_tools("critic_spec")
                    review = CriticAgent("SPEC").run_with_tools(self.ctx, self.llm, tools)
                    assert_type(review, "REVIEW")
                    self.ctx.spec_review = review
                    self.state_manager.save_artifact("spec_review", review)
                    
                    if review.get("status") == "APPROVE":
                        self.state = State.PLAN
                    else:
                        self.state = State.SPEC_REPAIR
                
                elif self.state == State.SPEC_REPAIR:
                    if self.spec_repair_count >= self.max_spec_repairs:
                        print("[DEBUG] Max spec repairs reached.", file=sys.stderr)
                        self.state = State.FAILED
                        continue
                    
                    self.spec_repair_count += 1
                    tools = self.create_agent_tools("architect")
                    spec2 = self.repair_spec(self.ctx, self.ctx.spec_review, tools)
                    self.ctx.frozen_spec = spec2
                    self.state_manager.save_artifact(f"spec_repair_{self.spec_repair_count}", spec2)
                    self.state = State.SPEC_REVIEW
                
                elif self.state == State.PLAN:
                    tools = self.create_agent_tools("planner")
                    plan = PlannerAgent().run_with_tools(self.ctx, self.llm, tools)
                    assert_type(plan, "PLAN")
                    self.ctx.plan = plan
                    self.state_manager.save_artifact("plan", plan)
                    self.state = State.PATCH
                
                elif self.state == State.PATCH:
                    tools = self.create_agent_tools("coder")
                    patch = CoderAgent().run_with_tools(self.ctx, self.llm, tools)
                    assert_type(patch, "PATCH")
                    self.ctx.patches.append(patch)
                    self.state_manager.save_artifact(f"patch_{len(self.ctx.patches)}", patch)
                    self.state = State.APPLY
                
                elif self.state == State.APPLY:
                    tools = self.create_agent_tools("orchestrator")
                    try:
                        patch = self.ctx.patches[-1]
                        tools.apply_patch(patch)
                        
                        # Python Gate: Check existence and syntax
                        gate_runner = GateRunner(tools)
                        res = gate_runner.run_apply_gates(patch["files"])
                        
                        if res.passed:
                            self.state = State.PATCH_REVIEW
                        else:
                            print(f"[DEBUG] Apply Gate Failed: {res.reason}", file=sys.stderr)
                            # Create a failure report to guide repair
                            failure_report = {
                                "success": False,
                                "report": f"Apply Gate Check Failed.\nReason: {res.reason}\nEvidence: {json.dumps(res.evidence)}"
                            }
                            # Treat this as a test failure for repair purposes
                            self.ctx.test_reports.append(failure_report)
                            self.state = State.REPAIR_PATCH

                    except Exception as e:
                        print(f"[DEBUG] Failed to apply patch: {e}", file=sys.stderr)
                        self.state = State.FAILED
                
                elif self.state == State.PATCH_REVIEW:
                    tools = self.create_agent_tools("critic_patch")
                    review = CriticAgent("PATCH").run_with_tools(self.ctx, self.llm, tools)
                    assert_type(review, "REVIEW")
                    self.ctx.patch_review = review
                    self.state_manager.save_artifact("patch_review", review)
                    
                    # Policy: Critic is advisor only. Hard gates are handled in APPLY.
                    # We proceed to TEST regardless of Critic's opinion on style/logic,
                    # unless it identified a HARD BLOCKER (which we assume APPLY caught, but if not, we proceed to TEST anyway to fail deterministically).
                    print("[DEBUG] Critic Review saved. Proceeding to TEST (Advisor Mode).", file=sys.stderr)
                    self.state = State.TEST
                
                elif self.state == State.TEST:
                    tools = self.create_agent_tools("tester")
                    
                    # 1. Extract gates from SPEC
                    spec_gates = extract_gates_from_spec(self.ctx.frozen_spec or {}, self.ctx.packet.objective)
                    print(f"[DEBUG] Running Spec Gates: {spec_gates.keys()}", file=sys.stderr)

                    # 2. Run gates deterministically
                    gate_runner = GateRunner(tools)
                    results = gate_runner.run_spec_gates(spec_gates)
                    
                    # 3. Analyze results
                    all_passed = all(r.passed for r in results)
                    
                    report_text = "GATE EXECUTION REPORT:\n"
                    for i, r in enumerate(results):
                        status = "PASS" if r.passed else "FAIL"
                        report_text += f"{i+1}. [{status}] {r.reason}\n   Evidence: {json.dumps(r.evidence)}\n"
                    
                    test_report = {
                        "type": "TEST_REPORT",
                        "success": all_passed,
                        "report": report_text
                    }
                    
                    self.ctx.test_reports.append(test_report)
                    self.state_manager.save_artifact(f"test_report_{len(self.ctx.test_reports)}", test_report)
                    
                    if all_passed:
                        print("[DEBUG] All gates passed! Success.", file=sys.stderr)
                        self.state = State.DONE
                    else:
                        print("[DEBUG] Gates failed.", file=sys.stderr)
                        if self.repair_count < self.max_repairs:
                            self.repair_count += 1
                            self.state = State.REPAIR_PATCH
                        else:
                            print("[DEBUG] Max repairs reached.", file=sys.stderr)
                            self.state = State.FAILED
                
                elif self.state == State.REPAIR_PATCH:
                    tools = self.create_agent_tools("coder")
                    
                    if self.ctx.patch_review and self.ctx.patch_review.get("status") != "APPROVE":
                        print(f"[DEBUG] Repairing from Critic rejection...", file=sys.stderr)
                        patch2 = self.repair_patch(self.ctx, self.ctx.patch_review, tools)
                    else:
                        print(f"[DEBUG] Repairing from Test failure...", file=sys.stderr)
                        patch2 = self.repair_code_from_test(self.ctx, self.ctx.test_reports[-1], tools)
                    
                    self.ctx.patches.append(patch2)
                    self.state_manager.save_artifact(f"patch_repair_{len(self.ctx.patches)}", patch2)
                    self.state = State.PATCH_REVIEW
                
                # Save state after each step
                self.save_state()
                
            except Exception as e:
                print(f"[DEBUG] Error in state {self.state.name}: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                return 1
        
        # End of loop
        success = False
        if self.state == State.DONE:
             # Check if we actually succeeded in tests
             if self.ctx.test_reports and self.ctx.test_reports[-1].get("success"):
                 success = True
        
        if success:
            print("[DEBUG] Workflow SUCCESS", file=sys.stderr)
            sys.stderr.write(json.dumps(self.ctx.test_reports[-1], ensure_ascii=False, indent=2) + "\n")
            return 0
        else:
            print("[DEBUG] Workflow FAILED", file=sys.stderr)
            if self.ctx.test_reports:
                sys.stderr.write(json.dumps(self.ctx.test_reports[-1], ensure_ascii=False, indent=2) + "\n")
            elif self.state == State.FAILED:
                 sys.stderr.write(json.dumps({"error": "Workflow aborted due to repeated failures or critical error."}, ensure_ascii=False) + "\n")
            
            return 1
    
    def repair_code_from_test(self, ctx: RunContext, test_report: Dict[str, Any], 
                             tools: PersistentTools) -> Dict[str, Any]:
        files = tools.list_files()
        user = (
            f"Objective: {ctx.packet.objective}\n\n"
            f"FROZEN SPEC: {json.dumps(ctx.frozen_spec, ensure_ascii=False)}\n\n"
            f"Previous PATCHES: {len(ctx.patches)}\n"
            f"Workspace files: {files}\n\n"
            f"TEST REPORT (FAILURE): {json.dumps(test_report, ensure_ascii=False)}\n\n"
            "Please fix the code to satisfy the test report.\n"
            "Return PATCH JSON only."
        )
        patch = self.llm.chat_json(SYSTEM_CODER_REPAIR, user, temperature=DEFAULT_TEMPERATURE)
        assert_type(patch, "PATCH")
        return patch

    def repair_spec(self, ctx: RunContext, review: Dict[str, Any], 
                   tools: PersistentTools) -> Dict[str, Any]:
        user = (
            f"Objective: {ctx.packet.objective}\n\n"
            f"Previous SPEC: {json.dumps(ctx.frozen_spec, ensure_ascii=False)}\n\n"
            f"Critic review: {json.dumps(review, ensure_ascii=False)}\n\n"
            "Return corrected SPECIFICATION JSON only."
        )
        spec2 = self.llm.chat_json(SYSTEM_ARCHITECT, user, temperature=DEFAULT_TEMPERATURE)
        assert_type(spec2, "SPECIFICATION")
        return spec2

    def repair_patch(self, ctx: RunContext, review: Dict[str, Any], 
                    tools: PersistentTools) -> Dict[str, Any]:
        user = (
            f"Objective: {ctx.packet.objective}\n\n"
            f"FROZEN SPEC: {json.dumps(ctx.frozen_spec, ensure_ascii=False)}\n\n"
            f"Previous PATCH: {json.dumps(ctx.patches[-1], ensure_ascii=False)}\n\n"
            f"Critic review: {json.dumps(review, ensure_ascii=False)}\n\n"
            "Return corrected PATCH JSON only."
        )
        patch2 = self.llm.chat_json(SYSTEM_CODER_REPAIR, user, temperature=DEFAULT_TEMPERATURE)
        assert_type(patch2, "PATCH")
        return patch2
