---
name: jarvis-ops
description: JARVIS OMEGA operational utilities for security, quality, and lifecycle management. Use when auditing LLM performance, detecting hallucinations or prompt injections, managing LoRA adapters, or validating data quality.
---

# JARVIS Ops Skill

This skill provides a unified interface to the JARVIS OMEGA Quality Hub and its 10 specialized modules.

## Core Modules

1.  **Prompt Injection Detector**: `jarvis_prompt_injection_detector.py`
    - Use to check if a user input is a jailbreak or injection attempt.
2.  **Hallucination Detector**: `jarvis_hallucination_detector.py`
    - Use to verify factual consistency of LLM responses against provided context.
3.  **Content Moderator**: `jarvis_content_moderator.py`
    - Use to filter PII, toxicity, spam, and legal risks in real-time.
4.  **Evaluation Harness**: `jarvis_evaluation_harness.py`
    - Use to run automated benchmark suites on LLM nodes.
5.  **Model Committee**: `jarvis_model_committee.py`
    - Use to reach consensus among multiple models (qwen, deepseek, gemma) for high-reliability tasks.
6.  **LoRA Manager**: `jarvis_lora_manager.py`
    - Use to dynamically load/unload/switch fine-tuned adapters.
7.  **Online Learner**: `jarvis_online_learner.py`
    - Use to record feedback and adapt model selection strategies.
8.  **Golden Dataset**: `jarvis_golden_dataset.py`
    - Use to manage reference data samples for testing.
9.  **Regression Tester**: `jarvis_regression_tester.py`
    - Use to detect performance or quality drops compared to a baseline.
10. **Data Quality**: `jarvis_data_quality.py`
    - Use to profile and validate datasets for training or logs.

## Workflows

### Full Quality Audit
To perform a complete check on an LLM interaction:
1. Run `ContentModerator.moderate(input)`
2. Run `PromptInjectionDetector.detect(input)`
3. Query the LLM
4. Run `HallucinationDetector.detect(response, context)`
5. Run `RegressionTester.compare(...)` to check latency/score.

### Dataset Validation
To validate a new log file or training set:
1. Load data as a list of dicts.
2. Use `DataQualityChecker.check(data)`.
3. Analyze `QualityReport`.

## Reference Modules Location
All module source files are located in: `/home/turbo/IA/Core/jarvis/core/`
