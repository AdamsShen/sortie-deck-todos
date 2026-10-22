import type { HumanDecisionRecord, SubRoomRecord } from "./types";

/**
 * 前端演示用 mock 数据。
 *
 * 「小群」「决策中心」页面在后端无数据（返回空列表）时回退到这里的演示内容，
 * 便于 UI 走查与产品演示；后端有真实数据时完全不走本文件。
 * 截止时间均为「相对当前时刻」动态生成，保证倒计时/紧急度色条始终有演示效果。
 */

const minutesFromNow = (m: number) => new Date(Date.now() + m * 60_000).toISOString();

export const MOCK_SUBROOMS: SubRoomRecord[] = [
  {
    id: "mock-subroom-1",
    initiative_id: "camp-v2-01",
    parent_stage: "prd",
    initiator_role: "product",
    goal: "确认 App v2 首页改版的信息架构：保留「推荐流 + 精选栏目」双 Tab 还是收敛为单流",
    lead_role: "design",
    participants: ["design", "eng_web", "qa"],
    questions: ["双 Tab 还是单流？", "首屏信息密度上限几条卡片？"],
    max_rounds: 4,
    max_tokens: 12000,
    timeout_seconds: 300,
    depth: 1,
    status: "open",
    conclusion: "",
    token_estimate: 3600,
    reason: "prd 阶段自检发现两版信息架构方案各有利弊，拉小群对齐后再定稿",
    created_at: minutesFromNow(-30),
    updated_at: minutesFromNow(-2),
  },
  {
    id: "mock-subroom-2",
    initiative_id: "camp-v2-01",
    parent_stage: "eng_backend",
    initiator_role: "eng_backend",
    goal: "对齐支付回调幂等键的设计：以渠道流水号还是业务订单号作为唯一键",
    lead_role: "eng_backend",
    participants: ["product", "qa"],
    questions: ["幂等键用什么维度？", "回调重试窗口多久？"],
    max_rounds: 3,
    max_tokens: 9000,
    timeout_seconds: 240,
    depth: 1,
    status: "closed",
    conclusion:
      "以「渠道流水号 + 商户号」为幂等键，业务订单号做二级去重；重试窗口 24h，超窗落补偿表人工介入。结论已注入 prd→eng_backend 的阶段记忆。",
    token_estimate: 8200,
    reason: "渠道侧文档与内部订单模型不一致，需拉齐后才能进入编码",
    created_at: minutesFromNow(-180),
    updated_at: minutesFromNow(-140),
  },
  {
    id: "mock-subroom-3",
    initiative_id: "camp-v2-02",
    parent_stage: "design",
    initiator_role: "design",
    goal: "深色模式下的插画配色方案：沿用品牌蓝还是引入夜境紫渐变",
    lead_role: "design",
    participants: ["product", "eng_web"],
    questions: ["品牌蓝在暗底下的对比度是否达标？"],
    max_rounds: 3,
    max_tokens: 8000,
    timeout_seconds: 240,
    depth: 1,
    status: "timeout",
    conclusion: "",
    token_estimate: 2600,
    reason: "讨论超时未收敛，暂沿用品牌蓝方案，待下一轮设计评审复议",
    created_at: minutesFromNow(-600),
    updated_at: minutesFromNow(-540),
  },
];

/** mock 小群的本地演示对白（真实小群走 /api/subrooms/:id/transcript） */
export const MOCK_TRANSCRIPTS: Record<string, { role: string; text: string; at: string }[]> = {
  "mock-subroom-1": [
    {
      role: "product",
      text: "背景同步一下：数据侧反馈单流滑到第 5 屏流失明显，双 Tab 的精选栏目点击率不错但入口太深。大家倾向哪个方向？",
      at: minutesFromNow(-28),
    },
    {
      role: "design",
      text: "从视觉层级看双 Tab 更稳，但需要给精选栏目做独立的封面规范，成本大概多 2 天。",
      at: minutesFromNow(-24),
    },
    {
      role: "eng_web",
      text: "技术上单流对虚拟列表更友好；双 Tab 要做两套滚动容器，首屏 LCP 预估多 200ms。",
      at: minutesFromNow(-19),
    },
    {
      role: "qa",
      text: "提醒一下：无论哪种方案，回归用例要覆盖弱网下的 Tab 切换恢复。",
      at: minutesFromNow(-12),
    },
    {
      role: "product",
      text: "那就先按双 Tab 推进，首屏密度上限 6 条，LCP 的问题请 eng_web 出一版优化清单。",
      at: minutesFromNow(-3),
    },
  ],
  "mock-subroom-2": [
    {
      role: "eng_backend",
      text: "渠道文档里流水号在他们重试时会变，所以单纯用它做幂等键有风险。",
      at: minutesFromNow(-170),
    },
    {
      role: "product",
      text: "业务侧要求同一订单绝不能扣两次款，订单号维度必须有。",
      at: minutesFromNow(-160),
    },
    {
      role: "qa",
      text: "我会补一组「渠道重试 + 订单号不变」的并发用例。",
      at: minutesFromNow(-150),
    },
    {
      role: "eng_backend",
      text: "结论定了：渠道流水号 + 商户号做一级幂等，订单号做二级去重，超窗进补偿表。",
      at: minutesFromNow(-142),
    },
  ],
  "mock-subroom-3": [
    {
      role: "design",
      text: "夜境紫渐变在 OLED 上氛围感更强，但和品牌蓝混用会有色相冲突。",
      at: minutesFromNow(-580),
    },
    {
      role: "eng_web",
      text: "渐变图层会让低端机渲染帧率掉 5%，建议做成可降级资源。",
      at: minutesFromNow(-560),
    },
  ],
};

/**
 * mock 决策记录：3 条待拍板（覆盖红/琥珀/绿三档紧急度）+ 2 条历史。
 * deadline 动态计算，保证演示时倒计时始终有效。
 */
export function mockDecisions(): HumanDecisionRecord[] {
  return [
    {
      id: "mock-decision-1",
      decision_id: "dec-launch-sla",
      initiative_id: "camp-v2-01",
      stage: "launch",
      question: "渠道包提前到周四出包，还是等安全扫描全绿后再发？",
      context: "安全扫描目前 2 高危 1 中危，均为依赖库误报概率较高；渠道方周四截单。",
      options: ["周四提前出包", "等扫描全绿"],
      assignee: "沈石",
      channel: "ui",
      status: "pending",
      sla_seconds: 900,
      deadline_at: minutesFromNow(3),
      fallback_action: "pause_pipeline",
      created_at: minutesFromNow(-12),
      updated_at: minutesFromNow(-12),
    },
    {
      id: "mock-decision-2",
      decision_id: "dec-pay-idem",
      initiative_id: "camp-v2-01",
      stage: "eng_backend",
      question: "支付回调补偿表的兜底策略：自动重放还是仅告警人工处理？",
      context: "小群已定幂等键方案，超窗回调目前日均约 12 笔。",
      options: ["自动重放", "仅告警"],
      assignee: "李响",
      channel: "ui",
      status: "pending",
      sla_seconds: 1800,
      deadline_at: minutesFromNow(11),
      fallback_action: "auto_choose_default",
      created_at: minutesFromNow(-18),
      updated_at: minutesFromNow(-18),
    },
    {
      id: "mock-decision-3",
      decision_id: "dec-dark-theme",
      initiative_id: "camp-v2-02",
      stage: "design",
      question: "深色模式插画是否引入夜境紫渐变（需低端机做可降级资源）？",
      context: "小群讨论超时未收敛，升级人工拍板。",
      options: ["引入渐变", "沿用品牌蓝"],
      assignee: "王一诺",
      channel: "ui",
      status: "pending",
      sla_seconds: 3600,
      deadline_at: minutesFromNow(38),
      fallback_action: "skip_stage",
      created_at: minutesFromNow(-30),
      updated_at: minutesFromNow(-30),
    },
    {
      id: "mock-decision-4",
      decision_id: "dec-trk-express",
      initiative_id: "camp-v2-01",
      stage: "mission",
      question: "本次改版是否走 express 快线（跳过 QA 全量回归）？",
      context: "改动集中在首页样式层，接口无变更。",
      options: ["express", "standard"],
      assignee: "沈石",
      status: "answered",
      answer: "standard",
      answered_by: "沈石",
      answered_at: minutesFromNow(-95),
      sla_seconds: 1200,
      deadline_at: minutesFromNow(-80),
      fallback_action: "auto_choose_default",
      created_at: minutesFromNow(-100),
      updated_at: minutesFromNow(-95),
    },
    {
      id: "mock-decision-5",
      decision_id: "dec-copy-tone",
      initiative_id: "camp-v2-02",
      stage: "prd",
      question: "开屏文案基调：热血向还是治愈向？",
      context: "A/B 素材两组都已备好。",
      options: ["热血向", "治愈向"],
      assignee: "王一诺",
      status: "expired",
      sla_seconds: 600,
      deadline_at: minutesFromNow(-200),
      fallback_action: "auto_choose_default",
      fallback_reason: "超时未响应，已按默认「治愈向」继续",
      created_at: minutesFromNow(-220),
      updated_at: minutesFromNow(-200),
    },
  ];
}
