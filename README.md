# AI Agent Orchestration

This project implements an autonomous AI agent orchestration system designed to generate software artifacts through a structured workflow. It uses a multi-agent architecture to specify, plan, code, review, and test software solutions.

## Architecture

The system operates as a state machine with the following states:
- **SPEC**: Architect agent creates a software specification.
- **SPEC_REVIEW**: Critic agent reviews the specification.
- **PLAN**: Planner agent creates a step-by-step implementation plan.
- **PATCH**: Coder agent writes code (patches) to implement the plan.
- **PATCH_REVIEW**: Critic agent reviews the code patch.
- **APPLY**: Patches are applied to the workspace.
- **TEST**: Tester agent verifies the implementation.
- **REPAIR**: If tests or reviews fail, the system enters a repair loop.

## Setup

1.  Clone the repository.
2.  Install dependencies:
    ```bash
    pip install python-dotenv
    ```
    *Note: The project uses standard libraries, but `python-dotenv` is recommended for environment management.*
3.  Copy `env.example` to `.env`.
    ```bash
    cp env.example .env
    ```
4.  Configure your LLM credentials in `.env`.

## Usage

Run the orchestrator with a natural language objective:

```bash
python main_v2.py "Create a python script that prints the first 10 Fibonacci numbers"
```

### Resuming Tasks

To resume an interrupted task, set the `RESUME` environment variable or use the automatic resumption logic if the `task_id` matches.

## License

[MIT](LICENSE)
