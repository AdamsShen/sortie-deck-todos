"""WeKnora (Tencent OSS) HTTP client — import / catalog / hybrid retrieval.

Sortie Deck does not re-implement RAG. Search always uses WeKnora's hybrid-search.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class WeKnoraError(RuntimeError):
    pass


@dataclass
class WeKnoraHit:
    knowledge_id: str
    knowledge_title: str
    content: str
    score: float
    knowledge_filename: str = ""
    knowledge_base_id: str = ""
    chunk_id: str = ""
    match_type: int | None = None


@dataclass
class WeKnoraKnowledge:
    id: str
    title: str
    description: str = ""
    folder_path: str = ""
    knowledge_base_id: str = ""
    file_name: str = ""
    parse_status: str = ""
    enable_status: str = ""
    type: str = ""
    raw: dict[str, Any] | None = None


class WeKnoraClient:
    """Minimal subset of WeKnora REST API used by Sortie Deck."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        root = (base_url or "").strip().rstrip("/")
        if root.endswith("/api/v1"):
            self.base_url = root
        else:
            self.base_url = f"{root}/api/v1"
        self.api_key = (api_key or "").strip() or None
        self._owns_client = client is None
        # trust_env=False：忽略系统/环境代理。WeKnora 通常部署在本机或内网，
        # VPN 工具设置的 macOS 系统代理会把 127.0.0.1 的请求转发给代理进程，
        # 代理无法回连宿主机端口而返回 502（curl 不读系统代理所以看不出问题）。
        self._client = client or httpx.Client(timeout=timeout, trust_env=False)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> WeKnoraClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._headers()
        if json_body is not None and files is None:
            headers["Content-Type"] = "application/json"
        try:
            resp = self._client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                data=data,
                files=files,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise WeKnoraError(f"WeKnora request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise WeKnoraError(f"WeKnora {method} {path} → {resp.status_code}: {resp.text[:400]}")
        if not resp.content:
            return {}
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WeKnoraError(f"WeKnora returned non-JSON: {resp.text[:200]}") from exc
        if isinstance(payload, dict) and payload.get("success") is False:
            raise WeKnoraError(payload.get("message") or payload.get("code") or "WeKnora error")
        return payload

    def health(self) -> dict[str, Any]:
        # Prefer listing KBs as a connectivity probe
        kbs = self.list_knowledge_bases()
        return {"ok": True, "knowledge_base_count": len(kbs), "base_url": self.base_url}

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/knowledge-bases")
        data = payload.get("data") if isinstance(payload, dict) else payload
        return list(data or [])

    def list_knowledge(
        self,
        knowledge_base_id: str,
        *,
        page: int = 1,
        page_size: int = 100,
    ) -> list[WeKnoraKnowledge]:
        payload = self._request(
            "GET",
            f"/knowledge-bases/{knowledge_base_id}/knowledge",
            params={"page": page, "page_size": page_size},
        )
        rows = payload.get("data") if isinstance(payload, dict) else payload
        out: list[WeKnoraKnowledge] = []
        for row in rows or []:
            out.append(self._parse_knowledge(row, knowledge_base_id))
        return out

    def list_folders(self, knowledge_base_id: str) -> Any:
        payload = self._request("GET", f"/knowledge-bases/{knowledge_base_id}/knowledge/folders")
        return payload.get("data") if isinstance(payload, dict) else payload

    def hybrid_search(
        self,
        knowledge_base_id: str,
        query: str,
        *,
        match_count: int = 6,
        vector_threshold: float = 0.0,
        keyword_threshold: float = 0.0,
    ) -> list[WeKnoraHit]:
        payload = self._request(
            "POST",
            f"/knowledge-bases/{knowledge_base_id}/hybrid-search",
            json_body={
                "query_text": query,
                "match_count": match_count,
                "vector_threshold": vector_threshold,
                "keyword_threshold": keyword_threshold,
                # WeKnora 仅在请求显式携带 rerank 对象时才走重排路径（handler
                # knowledgebase.go: req.Rerank != nil 分支）；租户未配 rerank
                # 模型时 WeKnora 自动降级为 RRF 检索序，故这里可以无条件开启
                "rerank": {"enabled": True},
            },
        )
        rows = payload.get("data") if isinstance(payload, dict) else payload
        hits: list[WeKnoraHit] = []
        for row in rows or []:
            hits.append(
                WeKnoraHit(
                    chunk_id=str(row.get("id") or ""),
                    knowledge_id=str(row.get("knowledge_id") or ""),
                    knowledge_title=str(row.get("knowledge_title") or row.get("knowledge_filename") or ""),
                    content=str(row.get("content") or row.get("matched_content") or ""),
                    score=float(row.get("score") or 0.0),
                    knowledge_filename=str(row.get("knowledge_filename") or ""),
                    knowledge_base_id=str(row.get("knowledge_base_id") or knowledge_base_id),
                    match_type=row.get("match_type"),
                )
            )
        return hits

    def create_manual(
        self,
        knowledge_base_id: str,
        *,
        title: str,
        content: str,
        channel: str = "sortie",
    ) -> WeKnoraKnowledge:
        payload = self._request(
            "POST",
            f"/knowledge-bases/{knowledge_base_id}/knowledge/manual",
            json_body={"title": title, "content": content, "channel": channel},
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        return self._parse_knowledge(data or {}, knowledge_base_id)

    def create_from_url(
        self,
        knowledge_base_id: str,
        url: str,
        *,
        channel: str = "sortie",
    ) -> WeKnoraKnowledge:
        payload = self._request(
            "POST",
            f"/knowledge-bases/{knowledge_base_id}/knowledge/url",
            json_body={"url": url, "channel": channel},
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        return self._parse_knowledge(data or {}, knowledge_base_id)

    def create_from_file(
        self,
        knowledge_base_id: str,
        file_path: Path,
        *,
        file_name: str | None = None,
        channel: str = "sortie",
    ) -> WeKnoraKnowledge:
        path = Path(file_path)
        name = file_name or path.name
        with path.open("rb") as fh:
            files = {"file": (name, fh)}
            data = {"channel": channel, "fileName": name}
            payload = self._request(
                "POST",
                f"/knowledge-bases/{knowledge_base_id}/knowledge/file",
                data=data,
                files=files,
            )
        row = payload.get("data") if isinstance(payload, dict) else payload
        return self._parse_knowledge(row or {}, knowledge_base_id)

    def delete_knowledge(self, knowledge_id: str) -> None:
        self._request("DELETE", f"/knowledge/{knowledge_id}")

    def create_session(self, title: str = "sortie-retrieval") -> str:
        """创建 WeKnora 会话，返回 session_id。

        GraphRAG 增强（实体识别 + 图谱扩展召回）只挂在 knowledge-chat 管线上，
        hybrid-search 不经过该管线，因此 graph 模式检索需要一个会话容器。
        """
        payload = self._request(
            "POST", "/sessions", json_body={"title": (title or "sortie-retrieval")[:120]}
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        sid = (data or {}).get("id") if isinstance(data, dict) else None
        if not sid:
            raise WeKnoraError("create_session: missing session id in response")
        return str(sid)

    def delete_session(self, session_id: str) -> None:
        # 尽力而为：会话清理失败不影响检索结果
        with contextlib.suppress(Exception):
            self._request("DELETE", f"/sessions/{session_id}")

    def knowledge_chat_references(
        self,
        session_id: str,
        query: str,
        knowledge_base_ids: list[str],
        *,
        knowledge_ids: list[str] | None = None,
        timeout: float = 90.0,
    ) -> list[WeKnoraHit]:
        """走 knowledge-chat（GraphRAG 管线）检索，只取 references 事件转成命中块。

        流程：POST /knowledge-chat/{session_id} 返回 SSE 流；其中
        response_type=references 事件携带 knowledge_references（chunk 列表，
        含向量/关键词/图谱实体扩展三路召回的合并结果）。拿到该事件后立即
        断开连接，不等 LLM 生成完整回答，降低延迟与成本。

        输入：session_id（create_session 产生）、query、知识库 ID 列表、
              可选 knowledge_ids（限定具体文档，用于任务命名空间过滤）
        输出：WeKnoraHit 列表（与 hybrid_search 同构）
        异常：WeKnoraError（连接失败 / HTTP>=400 / 非法响应），由上层降级
        """
        body: dict[str, Any] = {
            "query": query,
            "knowledge_base_ids": knowledge_base_ids,
            "disable_title": True,
        }
        if knowledge_ids:
            body["knowledge_ids"] = knowledge_ids
        url = f"{self.base_url}/knowledge-chat/{session_id}"
        refs: list[dict[str, Any]] = []
        try:
            # SSE：拿到 references 事件即断开，避免等 LLM 生成正文
            with self._client.stream(
                "POST",
                url,
                headers={**self._headers(), "Content-Type": "application/json"},
                json=body,
                timeout=timeout,
            ) as resp:
                if resp.status_code >= 400:
                    raise WeKnoraError(f"WeKnora knowledge-chat -> {resp.status_code}")
                for line in resp.iter_lines():
                    line = (line or "").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        payload = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if payload.get("knowledge_references"):
                        refs = payload["knowledge_references"]
                    rtype = payload.get("response_type")
                    if rtype == "complete" or payload.get("done") is True:
                        if refs:
                            break
        except httpx.HTTPError as exc:
            raise WeKnoraError(f"WeKnora knowledge-chat stream failed: {exc}") from exc
        if not refs:
            raise WeKnoraError("WeKnora knowledge-chat: no references event received")
        hits: list[WeKnoraHit] = []
        for row in refs:
            hits.append(
                WeKnoraHit(
                    chunk_id=str(row.get("id") or row.get("sub_chunk_id") or ""),
                    knowledge_id=str(row.get("knowledge_id") or ""),
                    knowledge_title=str(row.get("knowledge_title") or ""),
                    content=str(row.get("matched_content") or row.get("content") or ""),
                    score=float(row.get("score") or 0.0),
                    knowledge_filename=str(row.get("knowledge_filename") or ""),
                    knowledge_base_id=str(row.get("knowledge_base_id") or ""),
                    match_type=row.get("match_type"),
                )
            )
        return hits

    def knowledge_base_detail(self, knowledge_base_id: str) -> dict[str, Any]:
        """获取知识库详情（含 capabilities，用于探活 GraphRAG 是否开启）。"""
        payload = self._request("GET", f"/knowledge-bases/{knowledge_base_id}")
        data = payload.get("data") if isinstance(payload, dict) else payload
        return data or {}

    def move_to_folder(
        self,
        *,
        knowledge_ids: list[str],
        folder_path: str,
        knowledge_base_id: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "knowledge_ids": knowledge_ids,
            "folder_path": folder_path,
        }
        if knowledge_base_id:
            body["knowledge_base_id"] = knowledge_base_id
        return self._request("POST", "/knowledge/folder", json_body=body)

    @staticmethod
    def _parse_knowledge(row: dict[str, Any], knowledge_base_id: str) -> WeKnoraKnowledge:
        return WeKnoraKnowledge(
            id=str(row.get("id") or ""),
            title=str(row.get("title") or row.get("file_name") or ""),
            description=str(row.get("description") or ""),
            folder_path=str(row.get("folder_path") or ""),
            knowledge_base_id=str(row.get("knowledge_base_id") or knowledge_base_id),
            file_name=str(row.get("file_name") or ""),
            parse_status=str(row.get("parse_status") or ""),
            enable_status=str(row.get("enable_status") or ""),
            type=str(row.get("type") or ""),
            raw=row,
        )
