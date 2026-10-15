"""GitLab REST API 客户端（自建/极狐均适用）：自动创建 MR。

仅实现任务回写链路所需的最小面：
- 按源分支查询 open MR（去重，避免重试风暴）；
- 创建 MR（source=任务分支，target=任务关联的目标分支）。

软降级约定：调用方（git_publish.publish_repos）捕获 GitLabError 后仅记日志/事件，
不回滚已完成的 push——MR 创建失败由人工在平台上手动建。
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class GitLabError(RuntimeError):
    """GitLab API 调用失败（网络/鉴权/业务错误统一归此）。"""


def project_path_from_url(repo_url: str) -> str | None:
    """从仓库 clone URL 解析 GitLab project path（URL-encoded 之前）。

    支持两种常见形式，解析失败返回 None（调用方软降级）::

        https://gitlab.example.com/group/sub/repo.git  → group/sub/repo
        git@gitlab.example.com:group/repo.git          → group/repo
    """
    url = (repo_url or "").strip()
    if not url:
        return None
    if url.startswith("git@"):
        host_sep = url.find(":", 4)
        if host_sep < 0:
            return None
        path = url[host_sep + 1 :]
    elif "://" in url:
        try:
            after = url.split("://", 1)[1]
            path = after.split("/", 1)[1] if "/" in after else ""
        except IndexError:
            return None
    else:
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path or None


class GitLabClient:
    """最小 GitLab v4 API 封装。同步 httpx（与 weknora.py 同模式，单次调用毫秒级）。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token}

    def _api(self, path: str) -> str:
        return f"{self.base_url}/api/v4{path}"

    def _do(self, method: str, path: str, **kwargs: object) -> dict:
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            resp = client.request(method, self._api(path), headers=self._headers(), **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise GitLabError(f"gitlab {method} {path} 网络失败: {exc}") from exc
        finally:
            if self._client is None:
                client.close()
        if resp.status_code >= 400:
            # 请求体可能含描述文本，只取关键片段，避免日志膨胀
            detail = resp.text[:300]
            raise GitLabError(f"gitlab {method} {path} -> HTTP {resp.status_code}: {detail}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise GitLabError(f"gitlab {method} {path} 响应非 JSON") from exc
        return data

    def find_open_mr(self, project_path: str, source_branch: str) -> str | None:
        """返回该源分支已存在的 open MR 的 web_url（去重用），无则 None。

        输入输出示例：find_open_mr("group/repo", "sortie/ini_abc") →
        "https://gitlab.example.com/group/repo/-/merge_requests/12" 或 None。
        """
        pid = quote(project_path, safe="")
        rows = self._do(
            "GET",
            f"/projects/{pid}/merge_requests",
            params={"state": "opened", "source_branch": source_branch, "per_page": "5"},
        )
        if isinstance(rows, list) and rows:
            return str(rows[0].get("web_url")) or None
        return None

    def create_mr(
        self,
        project_path: str,
        source_branch: str,
        target_branch: str | None,
        title: str,
        description: str = "",
    ) -> str:
        """创建 MR 并返回 web_url；已存在同源 open MR 时直接复用（幂等）。

        target_branch 传 None 表示目标仓库默认分支（GitLab 自动处理）。
        """
        existing = self.find_open_mr(project_path, source_branch)
        if existing:
            logger.info("gitlab MR 已存在（复用）: %s", existing)
            return existing
        pid = quote(project_path, safe="")
        payload: dict[str, str] = {
            "source_branch": source_branch,
            "title": title[:250],
        }
        if target_branch:
            payload["target_branch"] = target_branch
        if description:
            payload["description"] = description
        data = self._do("POST", f"/projects/{pid}/merge_requests", json=payload)
        web_url = str(data.get("web_url") or "")
        if not web_url:
            raise GitLabError("gitlab create_mr 响应缺少 web_url")
        logger.info("gitlab MR 已创建: %s", web_url)
        return web_url
