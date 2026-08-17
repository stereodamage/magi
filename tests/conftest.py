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


def rebuttal(updated_verdict="UNCHANGED", responses=()):
    return {"updated_verdict": updated_verdict, "responses": list(responses)}


def position(finding_id, position="ACCEPT", reason="r"):
    return {
        "finding_id": finding_id, "position": position,
        "reason": reason, "additional_evidence": [],
    }


class FakeBackend:
    def __init__(self, review=None, rebuttal_reply=None, fail=False, name="fake", model="fake-model"):
        self.review, self.fail, self.name, self.model = review, fail, name, model
        self.rebuttal_reply = rebuttal_reply or rebuttal()
        self.calls = []

    async def ask(self, prompt, *, system=None, schema=None, cwd=None, timeout=0.0,
                  on_progress=None):
        self.calls.append({"prompt": prompt, "system": system, "schema": schema, "cwd": cwd})
        if self.fail:
            raise BackendError("boom")
        from magi.council import REBUTTAL_SCHEMA

        payload = self.rebuttal_reply if schema is REBUTTAL_SCHEMA else self.review
        return Reply(self.name, json.dumps(payload), payload, 0.1)
