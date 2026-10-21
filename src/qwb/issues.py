"""Validation issue container shared by all validators."""

from __future__ import annotations

from dataclasses import dataclass, field

ERROR = "ERROR"  # result would be wrong; blocks training / packaging
WARN = "WARN"    # allowed, but shown on the report front page


@dataclass
class Issue:
    level: str
    code: str
    message: str
    count: int = 0               # number of offending rows, 0 if not row-based
    examples: list = field(default_factory=list)

    def __str__(self) -> str:
        n = f" ({self.count} rows)" if self.count else ""
        ex = f" e.g. {self.examples}" if self.examples else ""
        return f"[{self.level}] {self.code}: {self.message}{n}{ex}"


class Report:
    """Ordered list of issues with convenience accessors."""

    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def add(self, level: str, code: str, message: str, count: int = 0, examples=None) -> None:
        self.issues.append(Issue(level, code, message, count, list(examples or [])[:5]))

    def extend(self, other: "Report") -> None:
        self.issues.extend(other.issues)

    @property
    def ok(self) -> bool:
        return not any(i.level == ERROR for i in self.issues)

    def codes(self, level: str | None = None) -> set[str]:
        return {i.code for i in self.issues if level is None or i.level == level}

    def __str__(self) -> str:
        if not self.issues:
            return "OK: no issues"
        return "\n".join(str(i) for i in self.issues)
