"""Eval harness package for incident-agent.

Groups the pieces that make up `make eval`: scenarios.yaml (ground truth),
runner.py (orchestrates injecting faults and running the agent/baseline
pipelines against them), scorer.py (exact-match scoring + failure-mode
classification), baseline.py (the fixed-pipeline comparison point), and
judge.py (the optional LLM-as-judge explanation grader). This file has no
behavior of its own - it just makes `evals` importable as a package so
`evals.scorer`, `evals.baseline`, etc. can be imported from runner.py and
the test suite.
"""
