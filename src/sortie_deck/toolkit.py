from __future__ import annotations

import io
import json
import logging
import re
import threading
import zipfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from sortie_deck.models import new_id, utc_now

ToolkitKind = Literal["tool", "mcp", "skill"]
ToolRuntime = Literal["cli", "http", "function", "plugin"]
McpTransport = Literal["stdio", "sse", "http"]

logger = logging.getLogger(__name__)


class SkillScript(BaseModel):
    path: str
    content: str = ""
    language: str = ""  # py / sh / js / …
    description: str = ""


class ToolkitItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tk_"))
    kind: ToolkitKind
    name: str
    summary: str = ""
    # tool
    runtime: ToolRuntime | None = None
    endpoint: str = ""
    # mcp
    transport: McpTransport | None = None
    command: str = ""
    url: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # skill
    body: str = ""
    scripts: list[SkillScript] = Field(default_factory=list)
    # shared
    config: dict[str, Any] = Field(default_factory=dict)
    category: str = ""  # domain directory, e.g. engineering / product / qa
    tags: list[str] = Field(default_factory=list)
    featured: bool = False
    enabled: bool = True
    source: str = ""  # import provenance
    # --- loadout metadata -------------------------------------------------
    # Ability labels used by the JIT recall index (beyond name/tags/body)
    capabilities: list[str] = Field(default_factory=list)
    # May be executed / called at runtime (skill scripts, MCP tools)
    executable: bool = False
    # Write operations still need a human confirmation before running
    read_only: bool = True
    # 有状态 server（浏览器会话/登录态等）：池内每任务独占实例、不做空闲共享，
    # 防止跨任务状态泄露（对齐方案文档 3.5 节）
    stateful: bool = False
    author_id: str = ""
    author_name: str = ""
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


class CreateToolkitRequest(BaseModel):
    kind: ToolkitKind
    name: str
    summary: str = ""
    runtime: ToolRuntime | None = None
    endpoint: str = ""
    transport: McpTransport | None = None
    command: str = ""
    url: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    scripts: list[SkillScript] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    featured: bool = False
    enabled: bool = True
    source: str = ""
    capabilities: list[str] = Field(default_factory=list)
    executable: bool = False
    read_only: bool = True
    stateful: bool = False


class UpdateToolkitRequest(BaseModel):
    name: str | None = None
    summary: str | None = None
    runtime: ToolRuntime | None = None
    endpoint: str | None = None
    transport: McpTransport | None = None
    command: str | None = None
    url: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    body: str | None = None
    scripts: list[SkillScript] | None = None
    config: dict[str, Any] | None = None
    category: str | None = None
    tags: list[str] | None = None
    featured: bool | None = None
    enabled: bool | None = None
    source: str | None = None
    capabilities: list[str] | None = None
    executable: bool | None = None
    read_only: bool | None = None
    stateful: bool | None = None


class ImportToolkitRequest(BaseModel):
    kind: Literal["mcp", "skill"]
    content: str = ""
    filename: str = ""
    # when importing a zip, pass base64 without data: prefix
    zip_base64: str = ""
    overwrite: bool = False


# Domain directories for marketplace-style browsing (UI + optional API filter).
TOOLKIT_CATEGORIES: tuple[str, ...] = (
    "engineering",
    "product",
    "qa",
    "ops",
    "knowledge",
    "browser",
    "office",
)

_SEED: list[dict] = [
    {
        "kind": "tool",
        "name": "git-diff",
        "summary": "查看工作区 diff，供 ENG 专精位引用",
        "runtime": "cli",
        "endpoint": "git diff --stat",
        "category": "engineering",
        "tags": ["git", "eng"],
        "featured": True,
    },
    {
        "kind": "tool",
        "name": "http-probe",
        "summary": "探测预发健康检查",
        "runtime": "http",
        "endpoint": "GET /api/health",
        "category": "ops",
        "tags": ["deploy", "health"],
    },
    {
        "kind": "mcp",
        "name": "filesystem",
        "summary": "官方 filesystem MCP，读写仓库沙箱",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "./"],
        "category": "engineering",
        "tags": ["mcp", "fs"],
        "featured": True,
    },
    {
        "kind": "mcp",
        "name": "browser",
        "summary": "浏览器 MCP，用于验收截图与页面巡检",
        "transport": "sse",
        "url": "http://127.0.0.1:8931/sse",
        "category": "browser",
        "tags": ["mcp", "qa"],
    },
    {
        "kind": "skill",
        "name": "prd-tighten",
        "summary": "把松散需求压成可验收 PRD 的技能卡",
        "body": (
            "# Skill: PRD Tighten\n\n"
            "1. 抽出目标用户与成功指标\n"
            "2. 写清非目标\n"
            "3. 每条验收标准可测\n"
        ),
        "category": "product",
        "tags": ["product", "skill"],
        "featured": True,
    },
    {
        "kind": "skill",
        "name": "qa-clearance",
        "summary": "门禁测试技能：只按验收标准裁决",
        "body": (
            "# Skill: QA Clearance\n\n"
            "- 仅对照 acceptance 判定 pass/fail\n"
            "- fail 必须给出可回环的修复点\n"
        ),
        "scripts": [
            {
                "path": "scripts/check_acceptance.py",
                "language": "py",
                "description": "校验 acceptance.json 结构",
                "content": (
                    "#!/usr/bin/env python3\n"
                    "import json,sys\n"
                    "p=sys.argv[1] if len(sys.argv)>1 else 'acceptance.json'\n"
                    "data=json.load(open(p))\n"
                    "assert isinstance(data.get('criteria'), list), 'criteria must be list'\n"
                    "print('ok', len(data['criteria']))\n"
                ),
            }
        ],
        "category": "qa",
        "tags": ["qa", "skill"],
    },
]


def _guess_lang(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".py": "py",
        ".sh": "sh",
        ".bash": "sh",
        ".js": "js",
        ".ts": "ts",
        ".rb": "rb",
        ".go": "go",
        ".rs": "rs",
        ".md": "md",
    }.get(ext, "")


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    text = raw.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip("\"'")
    return meta, parts[2].lstrip("\n")


def parse_mcp_config(raw: str) -> list[CreateToolkitRequest]:
    data = json.loads(raw)
    if isinstance(data, list):
        servers = {f"mcp-{i}": x for i, x in enumerate(data) if isinstance(x, dict)}
    elif isinstance(data, dict):
        servers = data.get("mcpServers") or data.get("servers") or data
        if not isinstance(servers, dict):
            raise ValueError("unsupported MCP JSON shape")
    else:
        raise ValueError("MCP import must be JSON object or array")

    out: list[CreateToolkitRequest] = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        # skip non-server keys sometimes present
        if name in {"mcpServers", "servers"} and "command" not in cfg and "url" not in cfg:
            continue
        command = str(cfg.get("command") or "")
        args = [str(a) for a in (cfg.get("args") or [])]
        url = str(cfg.get("url") or cfg.get("serverUrl") or "")
        env = {str(k): str(v) for k, v in (cfg.get("env") or {}).items()}
        transport: McpTransport = "stdio"
        if url:
            transport = "sse" if "sse" in url.lower() or cfg.get("transport") == "sse" else "http"
        if cfg.get("transport") in {"stdio", "sse", "http"}:
            transport = cfg["transport"]  # type: ignore[assignment]
        # compact command line for display when args present
        display_cmd = command
        if args and command:
            display_cmd = " ".join([command, *args])
        out.append(
            CreateToolkitRequest(
                kind="mcp",
                name=str(name),
                summary=str(cfg.get("description") or cfg.get("summary") or f"Imported MCP · {name}"),
                transport=transport,
                command=command or display_cmd,
                url=url,
                args=args,
                env=env,
                config=cfg,
                tags=["mcp", "imported"],
                source="import:mcp-json",
            )
        )
    if not out:
        raise ValueError("no MCP servers found in payload")
    return out


def parse_skill_markdown(raw: str, *, name_hint: str = "") -> CreateToolkitRequest:
    meta, body = _parse_frontmatter(raw)
    name = meta.get("name") or name_hint or "imported-skill"
    summary = meta.get("description") or meta.get("summary") or ""
    tags = ["skill", "imported"]
    if meta.get("tags"):
        tags.extend([t.strip() for t in re.split(r"[, ]+", meta["tags"]) if t.strip()])
    return CreateToolkitRequest(
        kind="skill",
        name=name,
        summary=summary,
        body=body.strip() or raw.strip(),
        tags=tags,
        source="import:skill-md",
    )


def parse_skill_package_files(files: dict[str, str], *, name_hint: str = "") -> CreateToolkitRequest:
    skill_md = ""
    skill_path = ""
    for path, content in files.items():
        norm = path.replace("\\", "/").lstrip("./")
        base = Path(norm).name.lower()
        if base == "skill.md":
            skill_md = content
            skill_path = norm
            break
    if not skill_md:
        # fallback: first markdown
        for path, content in files.items():
            if path.lower().endswith(".md"):
                skill_md = content
                skill_path = path
                break
    if not skill_md:
        raise ValueError("skill package missing SKILL.md")

    hint = name_hint
    if not hint and skill_path:
        parent = Path(skill_path).parent.name
        if parent and parent not in {".", ""}:
            hint = parent
    req = parse_skill_markdown(skill_md, name_hint=hint)
    scripts: list[SkillScript] = []
    for path, content in files.items():
        norm = path.replace("\\", "/").lstrip("./")
        if Path(norm).name.lower() == "skill.md":
            continue
        # keep scripts/ and other executable-ish files
        if (
            norm.startswith("scripts/")
            or "/scripts/" in norm
            or Path(norm).suffix.lower() in {".py", ".sh", ".bash", ".js", ".ts", ".rb"}
        ):
            scripts.append(
                SkillScript(
                    path=norm if norm.startswith("scripts/") else f"scripts/{Path(norm).name}",
                    content=content,
                    language=_guess_lang(norm),
                )
            )
    req.scripts = scripts
    req.source = "import:skill-package"
    return req


def parse_skill_zip_bytes(data: bytes, *, name_hint: str = "") -> CreateToolkitRequest:
    files: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if name.startswith("__MACOSX/") or "/." in f"/{name}":
                continue
            try:
                raw = zf.read(info)
                files[name] = raw.decode("utf-8")
            except UnicodeDecodeError:
                # skip binary blobs for now
                continue
    return parse_skill_package_files(files, name_hint=name_hint)


class ToolkitStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            items = [
                ToolkitItem(
                    kind=s["kind"],  # type: ignore[arg-type]
                    name=s["name"],
                    summary=s.get("summary", ""),
                    runtime=s.get("runtime"),  # type: ignore[arg-type]
                    endpoint=s.get("endpoint", ""),
                    transport=s.get("transport"),  # type: ignore[arg-type]
                    command=s.get("command", ""),
                    url=s.get("url", ""),
                    args=s.get("args", []),
                    env=s.get("env", {}),
                    body=s.get("body", ""),
                    scripts=[SkillScript.model_validate(x) for x in s.get("scripts", [])],
                    category=str(s.get("category") or ""),
                    tags=s.get("tags", []),
                    featured=bool(s.get("featured", False)),
                    author_name="Sortie",
                )
                for s in _SEED
            ]
            self._write(items)

    def _read(self) -> list[ToolkitItem]:
        return [ToolkitItem.model_validate(x) for x in json.loads(self.path.read_text(encoding="utf-8"))]

    def _write(self, items: list[ToolkitItem]) -> None:
        self.path.write_text(
            json.dumps([i.model_dump(mode="json") for i in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _mirror_sync(self, item: ToolkitItem | None, *, item_id: str = "", removed: bool = False) -> None:
        """条目变更后同步 WeKnora 镜像（方案 A：混合检索 + rerank）。

        失败仅记日志，绝不阻塞条目主流程；backend 非 weknora 时直接跳过。
        """
        try:
            from sortie_deck.settings import settings as _settings

            if (_settings.toolkit_recall_backend or "").strip().lower() != "weknora":
                return
            from sortie_deck.toolkit_index import mirror_remove, mirror_upsert

            if removed:
                mirror_remove(item_id)
                logger.info("toolkit mirror removed (id=%s)", item_id)
            elif item is not None:
                result = mirror_upsert(item)
                logger.info("toolkit mirror %s (%s)", result, item.id)
        except Exception:  # noqa: BLE001 — 镜像失败不影响条目主流程
            logger.warning(
                "toolkit mirror sync failed (id=%s removed=%s)",
                item_id or getattr(item, "id", ""),
                removed,
            )

    def list(
        self,
        *,
        kind: str | None = None,
        category: str | None = None,
        q: str | None = None,
        featured: bool | None = None,
    ) -> list[ToolkitItem]:
        with self._lock:
            items = self._read()
        out: list[ToolkitItem] = []
        for item in items:
            if kind and item.kind != kind:
                continue
            if category and (item.category or "").lower() != category.lower():
                continue
            if featured is True and not item.featured:
                continue
            if q:
                needle = q.lower()
                script_blob = " ".join(s.path + " " + s.content for s in item.scripts)
                blob = (
                    f"{item.name} {item.summary} {item.category} {' '.join(item.tags)} "
                    f"{item.body} {script_blob}"
                ).lower()
                if needle not in blob:
                    continue
            out.append(item)
        # Newest first, then pin featured to the front (stable).
        out.sort(key=lambda x: x.updated_at, reverse=True)
        out.sort(key=lambda x: 0 if x.featured else 1)
        return out

    def get(self, item_id: str) -> ToolkitItem:
        for item in self._read():
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def create(self, req: CreateToolkitRequest, *, author_id: str, author_name: str) -> ToolkitItem:
        item = ToolkitItem(
            kind=req.kind,
            name=req.name.strip(),
            summary=req.summary.strip(),
            runtime=req.runtime,
            endpoint=req.endpoint.strip(),
            transport=req.transport,
            command=req.command.strip(),
            url=req.url.strip(),
            args=list(req.args),
            env=dict(req.env),
            body=req.body,
            scripts=list(req.scripts),
            config=req.config,
            category=req.category.strip(),
            tags=[t.strip() for t in req.tags if t.strip()],
            featured=req.featured,
            enabled=req.enabled,
            source=req.source,
            capabilities=[c.strip() for c in req.capabilities if c.strip()],
            executable=req.executable,
            read_only=req.read_only,
            stateful=req.stateful,
            author_id=author_id,
            author_name=author_name,
        )
        with self._lock:
            items = self._read()
            items.append(item)
            self._write(items)
        self._mirror_sync(item)
        return item

    def upsert_by_name(
        self, req: CreateToolkitRequest, *, author_id: str, author_name: str, overwrite: bool
    ) -> ToolkitItem:
        with self._lock:
            items = self._read()
            for i, existing in enumerate(items):
                if existing.kind == req.kind and existing.name == req.name.strip():
                    if not overwrite:
                        raise ValueError(f"already exists: {req.kind}/{req.name}")
                    data = existing.model_dump()
                    patch = req.model_dump()
                    data.update(patch)
                    data["id"] = existing.id
                    data["author_id"] = author_id
                    data["author_name"] = author_name
                    data["updated_at"] = utc_now().isoformat()
                    items[i] = ToolkitItem.model_validate(data)
                    self._write(items)
                    self._mirror_sync(items[i])
                    return items[i]
        return self.create(req, author_id=author_id, author_name=author_name)

    def update(self, item_id: str, req: UpdateToolkitRequest) -> ToolkitItem:
        with self._lock:
            items = self._read()
            for i, item in enumerate(items):
                if item.id != item_id:
                    continue
                data = item.model_dump()
                patch = req.model_dump(exclude_unset=True)
                for key in ("name", "summary", "endpoint", "command", "url", "source", "category"):
                    if key in patch and patch[key] is not None:
                        patch[key] = str(patch[key]).strip()
                if "tags" in patch and patch["tags"] is not None:
                    patch["tags"] = [t.strip() for t in patch["tags"] if t.strip()]
                if "scripts" in patch and patch["scripts"] is not None:
                    patch["scripts"] = [SkillScript.model_validate(s).model_dump() for s in patch["scripts"]]
                data.update(patch)
                data["updated_at"] = utc_now().isoformat()
                items[i] = ToolkitItem.model_validate(data)
                self._write(items)
                self._mirror_sync(items[i])
                return items[i]
        raise KeyError(item_id)

    def delete(self, item_id: str) -> None:
        with self._lock:
            items = self._read()
            next_items = [i for i in items if i.id != item_id]
            if len(next_items) == len(items):
                raise KeyError(item_id)
            self._write(next_items)
            self._mirror_sync(None, item_id=item_id, removed=True)

    def import_payload(
        self,
        req: ImportToolkitRequest,
        *,
        author_id: str,
        author_name: str,
    ) -> list[ToolkitItem]:
        created: list[CreateToolkitRequest] = []
        if req.kind == "mcp":
            if not req.content.strip():
                raise ValueError("MCP import needs JSON content")
            created = parse_mcp_config(req.content)
        elif req.kind == "skill":
            if req.zip_base64.strip():
                import base64

                raw = base64.b64decode(req.zip_base64)
                created = [parse_skill_zip_bytes(raw, name_hint=Path(req.filename).stem)]
            elif req.filename.lower().endswith(".zip") and req.content:
                # content may be raw bytes mistakenly as latin-1 — prefer zip_base64
                raise ValueError("zip imports must use zip_base64")
            elif req.content.strip().startswith("{"):
                data = json.loads(req.content)
                if isinstance(data, dict) and "files" in data and isinstance(data["files"], dict):
                    files = {str(k): str(v) for k, v in data["files"].items()}
                    created = [parse_skill_package_files(files, name_hint=data.get("name") or Path(req.filename).stem)]
                else:
                    # treat as single skill json export
                    created = [
                        CreateToolkitRequest(
                            kind="skill",
                            name=str(data.get("name") or Path(req.filename).stem or "imported-skill"),
                            summary=str(data.get("summary") or data.get("description") or ""),
                            body=str(data.get("body") or ""),
                            scripts=[SkillScript.model_validate(s) for s in data.get("scripts") or []],
                            tags=list(data.get("tags") or ["skill", "imported"]),
                            source="import:skill-json",
                        )
                    ]
            else:
                created = [
                    parse_skill_markdown(
                        req.content, name_hint=Path(req.filename).stem if req.filename else ""
                    )
                ]
        else:
            raise ValueError("unsupported import kind")

        results: list[ToolkitItem] = []
        for item_req in created:
            results.append(
                self.upsert_by_name(
                    item_req,
                    author_id=author_id,
                    author_name=author_name,
                    overwrite=req.overwrite,
                )
            )
        return results
