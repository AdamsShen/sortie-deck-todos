# 设计文档

本目录放 **产品 / 系统设计**（为何这样建），与 [模块实现计划](../modules/auth.md)（如何落地）互补。

## 目录

| 文档 | 内容 |
|------|------|
| [项目解决了什么问题](problems-solved.md) | 痛点与改造前后对照图 |
| [改造方案介绍](transformation.md) | 改造原则与落地抓手 |
| [目标与非目标](goals.md) | 问题陈述、范围边界 |
| [航线与图编排](pipeline-and-graph.md) | 规划、并行分支、产物契约 |
| [人在环](human-in-the-loop.md) | 门禁、确认、权限档 |
| [威胁模型](threat-model.md) | 信任边界与安全假设 |
| [架构决策记录 ADR](adr/index.md) | 重大技术取舍 |

架构总览仍见 [architecture/overview.md](../architecture/overview.md)；后续 backlog 见仓库根目录 [`TODO.md`](../../TODO.md)。

## 阅读顺序

1. 项目解决了什么问题 / 改造方案介绍  
2. 目标与非目标  
3. 架构总览 + 数据流  
4. 航线与图编排、人在环  
5. 按需读 ADR  
