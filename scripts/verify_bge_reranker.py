#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地验证脚本：拉取 BAAI/bge-reranker-v2-m3 权重并跑重排，输出耗时与内存占用。
适用环境：macOS + Apple Silicon（16GB 统一内存）。

用法：
  pip install FlagEmbedding psutil
  python3 scripts/verify_bge_reranker.py

说明：
- 首次运行会自动下载权重（~2.27GB），「加载耗时」里包含下载时间，属正常现象；
  第二次运行走本地缓存，加载耗时才是真实启动时间。
- 国内网络默认走 hf-mirror.com 镜像加速，可用 HF_ENDPOINT 覆盖。
"""

import os

# 镜像开关必须放在 import transformers/FlagEmbedding 之前才生效
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import platform
import resource
import subprocess
import sys
import time

MODEL = "BAAI/bge-reranker-v2-m3"


def chip_name() -> str:
    """返回芯片型号字符串（仅 macOS 有效），用于确认 Apple Silicon"""
    try:
        return subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
        ).strip()
    except Exception:
        return platform.machine()


def rss_gb() -> float:
    """当前进程常驻内存（GB）。优先用 psutil，缺失时回退 resource 模块"""
    try:
        import psutil

        return psutil.Process().memory_info().rss / 1024**3
    except ImportError:
        # macOS 上 ru_maxrss 单位是字节（Linux 是 KB），此处只跑 macOS 所以直接除字节
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**3


def peak_rss_gb() -> float:
    """进程峰值内存（GB）"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**3


def main():
    print("=" * 64)
    print(
        f"[环境] Python {sys.version.split()[0]} | {platform.system()} "
        f"{platform.machine()} | {chip_name()}"
    )
    print(f"[环境] 启动时 RSS: {rss_gb():.2f} GB")
    print(f"[环境] HF_ENDPOINT = {os.environ.get('HF_ENDPOINT', '(官方源)')}")

    from FlagEmbedding import FlagReranker

    # use_fp16=True：半精度加载，权重内存约减半，Apple Silicon 上推理更快
    t0 = time.perf_counter()
    reranker = FlagReranker(MODEL, use_fp16=True)
    load_s = time.perf_counter() - t0
    print(
        f"[加载] {MODEL}\n"
        f"[加载] 耗时 {load_s:.1f}s | RSS {rss_gb():.2f} GB | 峰值 {peak_rss_gb():.2f} GB"
    )

    # 测试数据：1 个 query + 6 个候选文档，模拟 WeKnora 粗筛后的精排输入
    query = "什么是重排序模型"
    docs = [
        "Reranker 是交叉编码器，对 query-doc 对打相关性分数",
        "今天天气晴朗，适合户外运动",
        "重排序在 RAG 中用于精排，提升最终召回质量",
        "苹果公司发布了新款手机",
        "bge-reranker-v2-m3 是 BAAI 发布的多语言重排模型",
        "数据库索引可以加速查询",
    ]
    pairs = [[query, d] for d in docs]

    # 预热一次，排除首次调用的初始化开销
    reranker.compute_score(pairs[:1], normalize=True)

    # 单批重排计时：重复 5 次取平均
    n_runs = 5
    t0 = time.perf_counter()
    for _ in range(n_runs):
        scores = reranker.compute_score(pairs, normalize=True)
    avg_s = (time.perf_counter() - t0) / n_runs

    print(f"[重排] 6 个候选，平均 {avg_s * 1000:.0f} ms/次")
    print(f"[内存] 重排后 RSS {rss_gb():.2f} GB | 峰值 {peak_rss_gb():.2f} GB")

    print(f"[结果] query: {query}")
    ranked = sorted(zip(scores, (d[:32] for d in docs)), reverse=True)
    for rank, (s, doc) in enumerate(ranked, 1):
        print(f"  Top{rank}  score={s:.4f}  {doc}")

    # 压力参考：50 候选单次耗时，模拟真实粗筛规模（最终返回 Top-5 时粗筛常取 50~100）
    long_pairs = (pairs * 8 + pairs[:2])[:50]
    t0 = time.perf_counter()
    reranker.compute_score(long_pairs, normalize=True)
    print(f"[重排] 50 个候选单次耗时 {(time.perf_counter() - t0) * 1000:.0f} ms")
    print("=" * 64)


if __name__ == "__main__":
    main()
