# AI Agent Orchestration Framework

This project implements a robust, state-machine-driven AI agent orchestration system designed to autonomously generate, review, and test software artifacts. It features a sophisticated multi-agent architecture with persistent state management, enabling complex software development workflows.

## Features

- **Multi-Agent Architecture**: Specialized agents for different stages of the SDLC:
  - **Architect**: Drafts detailed software specifications.
  - **Critic**: Reviews specifications and code patches, enforcing strict quality and functional requirements.
  - **Planner**: Devises step-by-step implementation plans.
  - **Coder**: Implements the plan by generating code patches.
  - **Tester**: Verifies the implementation against the original requirements.
  - **Researcher**: Validates external resources like URLs and APIs.

- **Persistent State Management**:
  - Automatically saves the full execution context (specs, plans, patches, reviews) to `.agent_state/`.
  - Resumes interrupted tasks seamlessly.
  - Maintains a history of all generated artifacts.

- **Safety & Verification**:
  - **Auto-Research**: Automatically detects and validates URLs in specifications using the Researcher agent.
  - **Change Logging**: Creates snapshots of files before modification, allowing for safe rollbacks and diffs.
  - **Restricted Tooling**: Agents operate with defined permission scopes.

## Architecture

The system operates as a finite state machine:

1.  **SPEC**: Architect creates a specification.
2.  **SPEC_REVIEW**: Critic reviews the spec. If approved, proceeds to PLAN. If rejected, goes to SPEC_REPAIR.
    - *Auto-Research runs here to pre-validate external links.*
3.  **SPEC_REPAIR**: Architect refines the spec based on feedback.
4.  **PLAN**: Planner breaks down the spec into actionable steps.
5.  **PATCH**: Coder generates a file system patch (create/edit files).
6.  **APPLY**: The patch is applied to the workspace.
7.  **PATCH_REVIEW**: Critic examines the applied changes.
8.  **TEST**: Tester runs verification commands.
9.  **REPAIR_PATCH**: If tests or patch reviews fail, the Coder attempts to fix the issues.
10. **DONE**: Workflow completes successfully or errors out.

## Setup

1.  **Clone the repository**:
    ```bash
    git clone <repository-url>
    cd <repository-directory>
    ```

2.  **Install Dependencies**:
    The project uses standard Python libraries. `python-dotenv` is recommended for managing API keys.
    ```bash
    pip install python-dotenv
    ```

3.  **Configuration**:
    Copy `env.example` to `.env` and add your LLM API keys.
    ```bash
    cp env.example .env
    ```

## Usage

Run the orchestrator with a natural language objective:

```bash
python main.py "Create a python script that fetches the latest BTC price and saves it to a CSV file"
```

The system will create a task-specific directory (e.g., `task_<timestamp>`) or work in the current directory depending on configuration, creating a `.agent_state` folder to track progress.

## Project Structure

- `main.py`: Core entry point containing the `PersistentOrchestrator`, `StateManager`, and Agent definitions.
- `.agent_state/`: Directory where runtime context, artifacts, and file snapshots are stored.
- `logs/`: Execution logs (if configured).

## License

[MIT](LICENSE)
