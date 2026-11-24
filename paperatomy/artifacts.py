"""Find public code candidates from a PDF and inspect repository metadata."""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPO = re.compile(
    r"https?://(?:www\.)?(github\.com|gitee\.com|gitlab\.com)/"
    r"([\w.-]+)/([\w.-]+)", re.I | re.ASCII,
)
CODE_CONTEXT = re.compile(
    r"code.{0,80}(?:available|release|public)|(?:source code|artifact|implementation)"
    r".{0,80}(?:available|github|gitee|gitlab)|代码.{0,40}(?:公开|开源|仓库)", re.I,
)


def _candidate(url: str) -> dict | None:
    match = REPO.match(url)
    if not match:
        return None
    host, owner, name = match.groups()
    name = name.rstrip(".").removesuffix(".git")
    if (owner in {".", ".."} or name in {"", ".", ".."}
            or owner.lower() in {"topics", "orgs", "users", "groups", "explore"}):
        return None
    return {"host": host.lower(), "repo": f"{owner}/{name}",
            "url": f"https://{host.lower()}/{owner}/{name}"}


def discover(pdf) -> list[dict]:
    if pdf is None:
        return []
    import pymupdf

    candidates = {}
    with pymupdf.open(pdf) as document:
        for page in document:
            for link in page.get_links():
                uri = link.get("uri") or ""
                if not REPO.match(uri):
                    continue
                box = pymupdf.Rect(link["from"])
                box = pymupdf.Rect(max(0, box.x0 - 220), max(0, box.y0 - 34),
                                   min(page.rect.width, box.x1 + 45), box.y1 + 16)
                _add(candidates, uri, page.number + 1, page.get_textbox(box))
            for block in page.get_text("blocks"):
                content = " ".join(block[4].split())
                for match in REPO.finditer(content):
                    _add(candidates, match.group(), page.number + 1,
                         content[max(0, match.start() - 180):match.end() + 70])
    return sorted(candidates.values(), key=lambda item: not item["paper_code"])[:6]


def _add(candidates, url, page, context):
    item = _candidate(url)
    if item is None:
        return
    item.update(page=page, paper_code=bool(CODE_CONTEXT.search(" ".join(context.split()))))
    key = item["url"].lower()
    if key not in candidates or item["paper_code"] and not candidates[key]["paper_code"]:
        candidates[key] = item


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read(url: str, *, accept: str = "application/json") -> bytes:
    req = Request(url, headers={"User-Agent": "paperatomy-artifact-check", "Accept": accept})
    with build_opener(_NoRedirect()).open(req, timeout=5) as response:
        return response.read(196_608)


def search_title(title: str) -> list[dict]:
    query = re.sub(r'[^\w\s-]', ' ', title).strip()[:160]
    if len(query.split()) < 3:
        return []
    endpoint = "https://api.github.com/search/repositories?" + urlencode({
        "q": f'"{query}" in:description,readme', "per_page": 3,
    })
    data = json.loads(_read(endpoint))
    return [dict(repo, paper_code=False, page=None) for item in data.get("items", [])
            if (repo := _candidate(item.get("html_url", ""))) is not None]


def _public_git_head(url: str) -> tuple[str, str] | None:
    """Use the anonymous Git endpoint when the metadata API is rate-limited."""
    try:
        content = _read(url + ".git/info/refs?service=git-upload-pack",
                        accept="application/x-git-upload-pack-advertisement")
    except OSError:
        return None
    head = re.search(rb"([0-9a-f]{40}|[0-9a-f]{64}) HEAD(?:\x00|\r?\n)", content)
    if not head or b"# service=git-upload-pack" not in content:
        return None
    branch = re.search(rb"symref=HEAD:refs/heads/([^\s\x00]+)", content)
    return (head[1].decode("ascii"),
            branch[1].decode("utf-8", "replace") if branch else "待核对")


def inspect(candidate: dict, title: str) -> dict:
    from base64 import b64decode

    host, repo = candidate["host"], candidate["repo"]
    api = {
        "github.com": f"https://api.github.com/repos/{repo}",
        "gitee.com": f"https://gitee.com/api/v5/repos/{repo}",
        "gitlab.com": f"https://gitlab.com/api/v4/projects/{quote(repo, safe='')}",
    }[host]
    result = dict(candidate, access="unknown", checks=[])
    try:
        info = json.loads(_read(api))
    except HTTPError as exc:
        result["access"] = "unavailable" if exc.code in {401, 404, 410} else "unknown"
        result["detail"] = f"平台返回 HTTP {exc.code}"
        if exc.code in {403, 429} and (head := _public_git_head(candidate["url"])):
            revision, branch = head
            result.update(access="public", revision=revision, branch=branch,
                          detail="已通过匿名 Git 端点确认可访问。元数据 API 拒绝请求或限流，README 与许可证待核对。")
        return result
    except (OSError, ValueError):
        result["detail"] = "网络或平台响应异常，可以重试"
        return result
    if not isinstance(info, dict) or not info.get("default_branch"):
        result["detail"] = "平台未返回默认分支"
        return result
    if info.get("private") or info.get("visibility") in {"private", "internal"}:
        result["access"] = "unavailable"
        result["detail"] = "平台未标为公开仓库"
        return result
    license_info = info.get("license")
    license_name = (license_info.get("spdx_id") or license_info.get("name")
                    if isinstance(license_info, dict) else license_info)
    result.update(access="public", branch=info["default_branch"], license=license_name,
                  fork=bool(info.get("fork") or info.get("forked_from_project")))
    readme = ""
    try:
        if host == "gitlab.com":
            readme = _read(api + "/repository/files/README.md/raw?ref=HEAD").decode("utf-8", "replace")
        else:
            response = json.loads(_read(api + "/readme"))
            readme = b64decode(response.get("content") or "").decode("utf-8", "replace")
    except (OSError, ValueError):
        pass
    try:
        endpoint = (api + "/repository/commits/" + quote(info["default_branch"], safe="")
                    if host == "gitlab.com" else api + "/commits/" + quote(info["default_branch"], safe=""))
        commit = json.loads(_read(endpoint))
        revision = commit.get("sha") or commit.get("id")
        if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{7,64}", revision):
            result["revision"] = revision
    except (OSError, ValueError):
        pass
    if "revision" not in result and (head := _public_git_head(candidate["url"])):
        result["revision"] = head[0]

    def normalize(value):
        return " ".join(re.findall(r"\w+", value.casefold()))

    normalized_title = normalize(title)
    result["title_match"] = bool(len(normalized_title) > 20 and normalized_title in
                                 normalize(readme + " " + str(info.get("description") or "")))
    lines = [line.strip() for line in readme.splitlines() if line.strip()
             and not re.search(r"\]\(#", line)]
    for label, pattern in (
        ("环境与构建", r"dependenc|cmake|conda|docker|pip install|编译"),
        ("输入与数据", r"dataset|data availability|workflow\.dag|input file|数据集"),
        ("运行说明", r"\./deploy/|python .+\.py|usage|quickstart|quick start|示例|运行"),
        ("硬件与额外许可", r"gurobi|cuda|gpu|hardware|compiler|硬件"),
    ):
        matched = [line for line in lines if re.search(pattern, line, re.I)]
        excerpt = next((line for line in matched if not line.startswith("#")),
                       matched[0] if matched else "")[:180]
        result["checks"].append({"label": label, "snippet": excerpt})
    result["sample_note"] = next((line[:180] for line in lines
                                 if re.search(r"an example is provided|examples?[/\\]|样例", line, re.I)), "")
    return result
