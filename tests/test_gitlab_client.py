"""GitLab MR 客户端测试：创建、open MR 去重复用、project path 解析、错误抛出。"""

import httpx
import pytest

from sortie_deck.gitlab import GitLabClient, GitLabError, project_path_from_url


def test_project_path_from_url():
    assert project_path_from_url("https://gitlab.example.com/group/repo.git") == "group/repo"
    assert project_path_from_url("https://gitlab.example.com/g1/g2/repo") == "g1/g2/repo"
    assert project_path_from_url("git@gitlab.example.com:group/repo.git") == "group/repo"
    assert project_path_from_url("not-a-url") is None
    assert project_path_from_url("") is None


class _StubTransport(httpx.BaseTransport):
    """按路径前缀返回固定响应的 httpx mock 传输层。"""

    def __init__(self, routes: dict[tuple[str, str], object]) -> None:
        self.routes = routes

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # raw_path 保留 %2F 编码（.path 会被 httpx 归一化成 "/"，无法区分 project path）
        key = (request.method, request.url.raw_path.decode().split("?", 1)[0])
        payload = self.routes.get(key)
        if payload is None:
            return httpx.Response(404, json={"message": "not found"})
        if isinstance(payload, int):
            return httpx.Response(payload, json={"message": "boom"})
        return httpx.Response(200, json=payload)


def test_create_mr_dedupes_existing_open_mr():
    existing = {"web_url": "https://gitlab.example.com/group/repo/-/merge_requests/7"}
    client = GitLabClient(
        "https://gitlab.example.com",
        "tok",
        client=httpx.Client(
            transport=_StubTransport(
                {("GET", "/api/v4/projects/group%2Frepo/merge_requests"): [existing]}
            )
        ),
    )
    url = client.create_mr("group/repo", "sortie/ini_1", "main", "t")
    assert url == existing["web_url"]


def test_create_mr_posts_when_absent():
    calls: list[httpx.Request] = []
    routes = {
        ("GET", "/api/v4/projects/group%2Frepo/merge_requests"): [],
        ("POST", "/api/v4/projects/group%2Frepo/merge_requests"): {
            "web_url": "https://gitlab.example.com/group/repo/-/merge_requests/9"
        },
    }

    class _RecordingTransport(_StubTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                calls.append(request)
            return super().handle_request(request)

    client = GitLabClient(
        "https://gitlab.example.com",
        "tok",
        client=httpx.Client(transport=_RecordingTransport(routes)),
    )
    url = client.create_mr("group/repo", "sortie/ini_1", "main", "title")
    assert url.endswith("/9")
    assert len(calls) == 1
    body = calls[0].read().decode()
    assert '"source_branch"' in body and "sortie/ini_1" in body
    assert '"target_branch"' in body and "main" in body


def test_create_mr_omits_target_when_default():
    calls: list[httpx.Request] = []
    routes = {
        ("GET", "/api/v4/projects/group%2Frepo/merge_requests"): [],
        ("POST", "/api/v4/projects/group%2Frepo/merge_requests"): {"web_url": "https://x/mr/1"},
    }

    class _RecordingTransport(_StubTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                calls.append(request)
            return super().handle_request(request)

    client = GitLabClient(
        "https://gitlab.example.com",
        "tok",
        client=httpx.Client(transport=_RecordingTransport(routes)),
    )
    client.create_mr("group/repo", "sortie/ini_1", None, "t")
    assert b"target_branch" not in calls[0].read()


def test_api_error_raises_gitlab_error():
    client = GitLabClient(
        "https://gitlab.example.com",
        "tok",
        client=httpx.Client(
            transport=_StubTransport({("GET", "/api/v4/projects/group%2Frepo/merge_requests"): 500})
        ),
    )
    with pytest.raises(GitLabError):
        client.find_open_mr("group/repo", "sortie/ini_1")
