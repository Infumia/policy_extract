"""Semantic-ish version parsing and comparison."""

from __future__ import annotations

import re


def parse_version_parts(version: str) -> tuple[list[int], str]:
    """'1.2.3' -> ([1,2,3], ''); '1.2b' -> ([1,2], 'b')."""
    version = version.strip().lstrip("vV")
    match = re.match(r"^(\d+(?:\.\d+)*)(.*)$", version)
    if not match:
        return [], version
    nums = [int(part) for part in match.group(1).split(".")]
    return nums, match.group(2).strip()


def compare_versions(a: str, b: str) -> int:
    """-1 when a < b, 0 when equal, 1 when a > b. Missing parts count as 0."""
    nums_a, rest_a = parse_version_parts(a)
    nums_b, rest_b = parse_version_parts(b)
    width = max(len(nums_a), len(nums_b))
    nums_a += [0] * (width - len(nums_a))
    nums_b += [0] * (width - len(nums_b))
    if nums_a != nums_b:
        return -1 if nums_a < nums_b else 1
    if rest_a == rest_b:
        return 0
    if not rest_a:
        return 1
    if not rest_b:
        return -1
    return -1 if rest_a < rest_b else 1
