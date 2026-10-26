"""Pytest defaults: never call real Claude CLI / remote LLM during unit tests."""

from __future__ import annotations

import os

# Must run before LlmClient reads env in imported modules' later calls.
os.environ.setdefault("TDT_LLM_CLI_FALLBACK", "0")
os.environ.setdefault("TDT_PIPELINE_LLM", "0")
os.environ.setdefault("TDT_ROOM_LLM", "0")
os.environ.setdefault("TDT_DEFAULT_CODING_EXECUTOR", "mock")
# 单测不依赖外部服务：知识库退回本地、LLM key 置空、harness 固定 mock。
# harness 执行器（HarnessStageExecutor/_rag_brief）读的是全局 settings 而非注入的 cfg，
# 只改测试入参不够，必须在这里用环境变量压住 .env。
os.environ.setdefault("TDT_KB_BACKEND", "local")
# 工具箱召回同理：.env 开启 weknora 镜像后，单测若走真实 KB 会依赖外部服务且结果随库内容漂移
os.environ.setdefault("TDT_TOOLKIT_RECALL_BACKEND", "local")
os.environ.setdefault("TDT_LLM_API_KEY", "")
os.environ.setdefault("TDT_DEFAULT_HARNESS", "mock")
