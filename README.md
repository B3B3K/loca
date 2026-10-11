# loca

**Python abstraction and agentic behaviour layer for local models.**

A collection of Python scripts I use daily for local, agent-like workloads: an LLM plans, calls tools, observes results and iterates, all running on your own hardware with no cloud API.

> ! **Status:** Early development. Expect breaking changes and rough edges.

## Overview

`loca` wraps local model backends (currently Ollama) behind a small agent loop. Each script defines a task-specific agent (e.g. Android project development, APK analysis) that runs step by step within a configurable context, step and observation budget.

<!-- TODO: add a short architecture diagram or a one-paragraph description of the agent loop -->

## Key Features

- **Local-first**: runs entirely on consumer hardware via Ollama
- **Agentic loop**: multi-step reasoning with tool use and observations
- **Task-specific agents**: ready-made scripts for Android development and APK analysis
- **Tunable limits**: context size, max tokens, max steps, max observation length, temperature
- **Optional thinking mode**: enable reasoning with `--think`
- **Backend abstraction**: designed so other backends can be added later
- **Offloading support**: split work between GPU and a separate CPU machine

## Scripts

| Script | Purpose |
|---|---|
| `android-developer.py` | Agent for working on Android/Kotlin (`.kts`) projects |
| `apk-analyzer.py` | Agent for analyzing APKs using decompiled (JADX) sources |

## Requirements

- Python 3.10+ <!-- TODO: confirm version -->
- [Ollama](https://ollama.com/) installed and running
- A pulled model, e.g. `qwen3.5:latest`
- [JADX](https://github.com/skylot/jadx) (only for `apk-analyzer.py`)

```powershell
pip install -r requirements.txt
```
<!-- TODO: add requirements.txt or list dependencies -->

## Testing Hardware

| Component | Role |
|---|---|
| **NVIDIA RTX 4050 (6 GB VRAM)** | Main compute, VRAM fully utilized |
| **Intel Core i5 12th Gen** | Offloading and Ollama server |
| **24 GB RAM** | ~20 GB in use |

## Results

| Metric | Value |
|---|---|
| Typical runtime | ~2 hours per task |
| Throughput | ~1 token/s with offloading and thinking enabled |

This is a slow setup by design: the goal is capability on modest hardware, not speed. Better hardware will run considerably faster.

## Usage

### 1. Start the backend (Ollama)

```powershell
ollama run qwen3.5:latest
```

> Keeping this session open prevents Ollama from unloading the model and exiting on its own.

### 2. Run an agent

**Android developer agent**

```powershell
python .\android-developer.py C:\project-with-kts `
  --backend ollama `
  --model-name qwen3.5:latest `
  --ctx 49152 `
  --max-tokens 8192 `
  --max-steps 40 `
  --max-obs 8000 `
  --temp 0.4 `
  -g "<task>"
```

**APK analyzer agent**

```powershell
python .\apk-analyzer.py base.apk `
  --src-dir .\base_work\jadx `
  --backend ollama `
  --model-name qwen3.5:latest `
  --ctx 49152 `
  --max-tokens 1500 `
  --max-steps 40 `
  --max-obs 8000 `
  --temp 0.4 `
  --think `
  -g "<task>"
```

## Arguments

| Argument | Description |
|---|---|
| `--backend` | Model backend (currently `ollama`) |
| `--model-name` | Model identifier, e.g. `qwen3.5:latest` |
| `--ctx` | Context window size (tokens) |
| `--max-tokens` | Max tokens generated per step |
| `--max-steps` | Max agent loop iterations |
| `--max-obs` | Max characters of tool output fed back to the model |
| `--temp` | Sampling temperature |
| `--think` | Enable thinking/reasoning mode |
| `-g` | Goal / task description for the agent |
| `--src-dir` | Decompiled source directory (APK analyzer) |

<!-- TODO: verify argument descriptions against the code -->

## Output Format
There is no logging and creating debug notifications but you can ask for reports in your prompt

## Configuration Tips

- **Low VRAM (6 GB)**: keep `--ctx` and `--max-tokens` moderate; larger values force offloading and slow generation.
- **Determinism**: lower `--temp` (0.2–0.4) works better for tool-calling agents.
- **Runaway loops**: use `--max-steps` and `--max-obs` to cap cost and context growth.


