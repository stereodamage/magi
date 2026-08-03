"""Shared synthetic-test helpers: fake backends, canned reviews. No live calls."""

import json

from magi.backends import BackendError, Reply


def review(verdict="APPROVE", findings=()):
    return {
        "verdict": verdict,
        "findings": list(findings),
        "unverified_hypotheses": [],
        "questions": [],
        "residual_risks": [],
    }


def finding(severity="high", id="M-001"):
    return {
        "id": id, "title": "t", "category": "correctness", "severity": severity,
        "confidence": 0.9, "file": "f.py", "start_line": 1, "end_line": 2,
        "trigger": "x", "observed_failure": "y", "evidence": ["e"],
        "violated_contract": "c", "suggested_direction": "d", "verification": "v",
    }


class FakeBackend:
    def __init__(self, review=None, fail=False, name="fake", model="fake-model"):
        self.review, self.fail, self.name, self.model = review, fail, name, model
        self.calls = []

    async def ask(self, prompt, *, system=None, schema=None, cwd=None, timeout=0.0):
        self.calls.append({"prompt": prompt, "system": system, "schema": schema, "cwd": cwd})
        if self.fail:
            raise BackendError("boom")
        return Reply(self.name, json.dumps(self.review), self.review, 0.1)
