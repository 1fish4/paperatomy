import base64
import json
from urllib.error import HTTPError

import pymupdf

from paperatomy import artifacts
from paperatomy.method_lab import asap_source, method_digest, method_profile
from paperatomy.scheduling import teaching_case


def test_teaching_trace_preserves_dependencies_and_deadline():
    demo = teaching_case()
    assert demo["first_difference"] == 1
    assert len(demo["lanes"]) == 2
    for lane in demo["lanes"]:
        previous = {}
        available_at = {}
        for step, task in zip(lane["rounds"], demo["tasks"], strict=True):
            current = step["slots"][-1]
            assert step["task"] in step["ready"]
            assert current["task"] == task["name"]
            assert current["end"] - current["start"] == task["duration"]
            assert current["start"] >= available_at.get(task["machine"], 0)
            assert all(current["start"] >= previous[parent] for parent in task["predecessors"])
            assert current["end"] <= demo["deadline"]
            assert step["earliest"] <= current["start"] <= step["latest"]
            available_at[task["machine"]] = current["end"]
            previous[task["name"]] = current["end"]
    left, right = [lane["rounds"][-1]["slots"] for lane in demo["lanes"]]
    assert sum(slot["green"] for slot in right) > sum(slot["green"] for slot in left)


def test_pdf_discovery_prefers_explicit_code_link(tmp_path):
    pdf = tmp_path / "paper.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((40, 60), "Source code is available at https://github.com/author/scheduler.git")
        page.insert_text((40, 90), "See https://github.com/thirdparty/utils for other tools.")
        page.insert_text((40, 125), "The ASAP baseline algorithm starts each task at its earliest")
        page.insert_text((40, 143), "possible start time.")
        doc.save(pdf)
    repos = artifacts.discover(pdf)
    assert asap_source(pdf) == 1
    assert repos[0]["url"] == "https://github.com/author/scheduler"
    assert (repos[0]["page"], repos[0]["paper_code"]) == (1, True)
    assert repos[1]["paper_code"] is False


def test_repository_check_uses_public_metadata_and_retains_missing_items(monkeypatch):
    readme = """# A Workflow Method for Deadline Scheduling
    - [Usage](#usage)
    CMake 3.10+ and a C++17 compiler
    ./deploy/schedule input.dot
    """
    endpoints = {
        "": {"default_branch": "master", "private": False, "license": {"spdx_id": "MIT"}},
        "/readme": {"content": base64.b64encode(readme.encode()).decode()},
        "/commits/master": {"sha": "abc1234"},
    }
    prefix = "https://api.github.com/repos/author/scheduler"

    def read(url):
        assert url.startswith(prefix)
        return json.dumps(endpoints[url[len(prefix):]]).encode()

    monkeypatch.setattr(artifacts, "_read", read)
    candidate = artifacts._candidate("https://github.com/author/scheduler")
    info = artifacts.inspect(candidate, "A Workflow Method for Deadline Scheduling")
    assert info["access"] == "public"
    assert info["title_match"]
    assert info["revision"] == "abc1234"
    assert not info["checks"][1]["snippet"]
    assert info["checks"][2]["snippet"] == "./deploy/schedule input.dot"

    def limited(url, **kwargs):
        raise HTTPError(url, 429, "rate limited", {}, None)

    monkeypatch.setattr(artifacts, "_read", limited)
    assert artifacts.inspect(candidate, "paper")["access"] == "unknown"

    def advertised(url, *, accept="application/json"):
        if "info/refs" in url:
            return b"001e# service=git-upload-pack\n0000" + b"a" * 40 + b" HEAD\x00symref=HEAD:refs/heads/main\n"
        raise HTTPError(url, 403, "rate limited", {}, None)

    monkeypatch.setattr(artifacts, "_read", advertised)
    fallback = artifacts.inspect(candidate, "paper")
    assert fallback["access"] == "public"
    assert fallback["revision"] == "a" * 40
    assert fallback["branch"] == "main"


def test_method_comparison_reads_steps_and_keeps_only_verified_page_links():
    data = {"paper_title": "Scheduling",
            "findings": [{"block_id": "method_steps", "content": "Compare time slots",
                          "evidence": [{"page": 3, "verified": True},
                                       {"page": 4, "verified": False}]}]}
    steps = method_profile(data)[2]
    assert steps["pages"] == [3]
    assert "Compare time slots" in method_digest(data)
