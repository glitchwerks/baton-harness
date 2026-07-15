"""symphony/tracker.py — GitHub Issues client via gh CLI."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime


class TrackerError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class Issue:
    number: int
    title: str
    state: str
    body: str
    url: str
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def identifier(self) -> str:
        return str(self.number)

    @classmethod
    def from_gh(cls, raw: dict) -> Issue:
        labels = [l["name"].lower() for l in raw.get("labels", [])]
        assignees = [a.get("login", "") for a in raw.get("assignees", [])]
        created_at = None
        if raw.get("createdAt"):
            try:
                created_at = datetime.fromisoformat(
                    raw["createdAt"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass
        updated_at = None
        if raw.get("updatedAt"):
            try:
                updated_at = datetime.fromisoformat(
                    raw["updatedAt"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass
        return cls(
            number=raw["number"],
            title=raw["title"],
            state=raw["state"].lower(),
            body=raw.get("body") or "",
            url=raw.get("url", ""),
            labels=labels,
            assignees=assignees,
            created_at=created_at,
            updated_at=updated_at,
        )


async def run_gh(args: list[str]) -> str:
    """Run a gh CLI command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise TrackerError(
            "gh_command_failed",
            f"gh {' '.join(args)} failed (rc={proc.returncode}): {stderr.decode().strip()}",
        )
    return stdout.decode()


def parse_issue_skills(body: str) -> list[str]:
    """Extract skill names from a ## Skills section in the issue body."""
    if not body:
        return []
    match = re.search(
        r"##\s*Skills\s*\n((?:\s*-\s*.+\n?)+)", body, re.IGNORECASE
    )
    if not match:
        return []
    lines = match.group(1).strip().split("\n")
    skills = []
    for line in lines:
        line = line.strip()
        if line.startswith("- "):
            skill = line[2:].strip()
            # Take only the skill name, ignore parenthetical descriptions
            skill = re.split(r"\s*\(", skill)[0].strip()
            if skill:
                skills.append(skill)
    return skills


class GitHubTracker:
    def __init__(
        self,
        labels: list[str] | None = None,
        exclude_labels: list[str] | None = None,
        assignee: str | None = None,
    ):
        self.labels = labels or []
        self.exclude_labels = [l.lower() for l in (exclude_labels or [])]
        self.assignee = assignee

    async def fetch_candidates(self) -> list[Issue]:
        """Fetch open issues matching configured filters."""
        args = [
            "issue",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,state,labels,body,url,createdAt,updatedAt,assignees",
            "--limit",
            "100",
        ]
        for label in self.labels:
            args.extend(["--label", label])
        if self.assignee:
            args.extend(["--assignee", self.assignee])

        output = await run_gh(args)
        raw_issues = json.loads(output)

        issues = [Issue.from_gh(r) for r in raw_issues]

        # Apply exclude filter
        if self.exclude_labels:
            issues = [
                i
                for i in issues
                if not any(el in i.labels for el in self.exclude_labels)
            ]

        # Sort: created_at ascending (oldest first)
        issues.sort(key=lambda i: i.created_at or datetime.min)
        return issues

    async def fetch_issue_state(self, number: int) -> str:
        """Fetch current state of a single issue."""
        output = await run_gh(
            [
                "issue",
                "view",
                str(number),
                "--json",
                "number,state",
            ]
        )
        raw = json.loads(output)
        return raw["state"].lower()

    async def fetch_issue_states(self, numbers: list[int]) -> dict[int, str]:
        """Fetch current states for multiple issues."""
        results = {}
        for number in numbers:
            try:
                results[number] = await self.fetch_issue_state(number)
            except TrackerError:
                pass
        return results

    async def check_pr_exists(self, issue_number: int) -> bool:
        """Check if an open PR exists that references this issue."""
        try:
            output = await run_gh(
                [
                    "pr",
                    "list",
                    "--state",
                    "open",
                    "--json",
                    "number,title,body,headRefName",
                    "--limit",
                    "50",
                ]
            )
            prs = json.loads(output)
            issue_ref = f"#{issue_number}"
            branch_suffix = f"-{issue_number}"
            for pr in prs:
                head = pr.get("headRefName", "")
                # Match by branch name pattern (baton/*-{number}) or issue reference
                if head.startswith("baton/") and head.endswith(branch_suffix):
                    return True
                if issue_ref in pr.get("title", "") or issue_ref in (
                    pr.get("body") or ""
                ):
                    return True
            return False
        except TrackerError:
            return False
