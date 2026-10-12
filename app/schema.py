"""The data contract (docs/SPEC.md, «Контракт данных»; docs/SPEC-analyst.md): JSON schemas the
models answer with (strict ``json_schema`` in ``response_format``) and the limits code enforces.

Every object lists all its properties in ``required`` and forbids extra ones: strict mode of the
gateway's OpenAI-compatible API needs that. Optional values are ``null``, never a missing key, so a
step always carries both ``next`` and ``branches``; app/checks.py turns the answer into the
contract's shape (``branches`` for a decision, ``next`` for the rest). ``maxItems`` and
``maxLength`` are not sent (not every model behind the gateway accepts them in strict mode): the
limits live in LIMITS and app/checks.py enforces them.
"""

from __future__ import annotations

import re


def _obj(props: dict, name: str | None = None) -> dict:
    schema = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    return {"name": name, "strict": True, "schema": schema} if name else schema


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


def _enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


STR = {"type": "string"}
NULLABLE_STR = {"type": ["string", "null"]}
NULLABLE_NUM = {"type": ["number", "null"]}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}

KINDS = ("start", "task", "decision", "end")
ACTIONS = ("keep", "automate", "remove", "merge")
# What «Стало» may do with a step of each kind (docs/SPEC.md, «Improve»).
ALLOWED_ACTIONS = {"start": ("keep",), "end": ("keep",), "decision": ("keep", "automate"), "task": ACTIONS}

STEP_ID = re.compile(r"s(?:[1-9]|1[0-5])")
ROLE_ID = re.compile(r"r[1-6]")

# Step 1. Text -> Process.
PROCESS = _obj({
    "title": STR,
    "roles": _arr(_obj({"id": STR, "name": STR, "external": BOOL})),  # external: outside the company
    "steps": _arr(_obj({
        "id": STR,
        "kind": _enum(*KINDS),
        "name": STR,
        "role": STR,
        "next": _arr(STR),                                    # start, task: one id; end, decision: []
        "branches": _arr(_obj({"label": STR, "to": STR})),    # decision: 2–3; the rest: []
        "quote": NULLABLE_STR,
        "tool": NULLABLE_STR,
        "minutes": NULLABLE_NUM,
        "wait_hours": NULLABLE_NUM,
    })),
}, "process")

# Step 2. Process -> Improve.
IMPROVE = _obj({
    "changes": _arr(_obj({
        "step": STR,
        "action": _enum(*ACTIONS),
        "merge_into": NULLABLE_STR,
        "how": STR,
        "risk": STR,
    })),
    "summary": STR,
}, "improve")

# The interview. Numbers of an answer the draft model may give: how many times a month the process
# runs, the price of an hour, people in a role, minutes of a step, hours of waiting before a step.
FIELDS = ("volume", "rate", "people", "minutes", "wait_hours")

# A draft after every answer: the Process of what is said so far plus the numbers of the last answer.
DRAFT = _obj({
    **PROCESS["schema"]["properties"],
    "answers": _arr(_obj({
        "field": _enum(*FIELDS),
        "target": NULLABLE_STR,   # a step id for minutes and wait_hours, a role id for people
        "value": NUM,
        "quote": STR,
    })),
}, "draft")

# The wording of the question the code chose.
QUESTION = _obj({"text": STR}, "question")

SCHEMAS = {"extract": PROCESS, "improve": IMPROVE, "draft": DRAFT, "ask": QUESTION}

LIMITS = {
    "steps": 15, "roles": 6, "branches_min": 2, "branches_max": 3,
    "title": 80, "step_name": 80, "role_name": 40, "label": 40, "tool": 40,
    "quote": 120, "how": 140, "risk": 140, "summary": 140,
    "minutes": 10_000, "wait_hours": 2_000,  # a bigger number is not a step of a process
}
