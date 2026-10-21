# LitLoom Agent 生态选型与接入边界

这份记录用于长期维护时判断“应该调用成熟组件，还是保留 LitLoom 本地实现”。目标是让论文检索、证据阅读和多年任务恢复可持续，不因为追逐框架而破坏本地数据和可审计性。

## 已核对的项目

以下信息来自项目官方文档或 PyPI 项目元数据，核对日期为 2026-10-21。版本号不是 LitLoom 的运行时依赖承诺；发布前仍需按锁定版本重新检查兼容性、许可证和安全公告。

| 项目 | 当前核对版本 | 适合 LitLoom 的场景 | 当前决定 |
|---|---:|---|---|
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) | 1.2.16 | 带检查点的长流程、条件分支、人工暂停和恢复 | 暂不替换现有 DAG。LitLoom 已把 Agent 步骤、依赖、预算、重试、确认和 SQLite 恢复作为持久数据；未来迁移时只允许 LangGraph 作为编排执行器，数据库契约仍是权威来源。 |
| [PydanticAI](https://ai.pydantic.dev/) | 2.58.0 | 类型化 Agent、结构化输出、依赖注入、工具错误处理 | 适合新模型任务的边界层。当前保留已有 JSON contract 和归一化器，避免把 Python 3.10 的本地部署强制升级到其当前 Python 3.11 要求。 |
| [LiteLLM](https://docs.litellm.ai/) | 1.106.0 | 多供应商网关、fallback、预算、统一模型遥测 | 适合团队或多供应商部署的可选旁路网关。LitLoom 当前已经支持 OpenAI-compatible 中转、任务模型路由、超时、重试和调用遥测；单机默认不启动额外代理，减少凭据和故障面。 |
| [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/) | 2.3.0 | 标准 MCP server/client、stdio、Streamable HTTP、SSE | 当前保留无第三方依赖的 stdio 适配器，已覆盖工具 schema、分页、异步任务状态、确认和错误映射。未来需要 Streamable HTTP 或官方 SDK 的传输兼容性时，再增加可选 `mcp` extra，不影响零依赖主程序。 |
| [Ragas](https://docs.ragas.io/) | 0.4.3 | RAG 的 context precision/recall、faithfulness 和回归评测 | 不放进在线请求路径。LitLoom 先用本地人工标注集评估 BM25、Embedding 与混合排序；有稳定标注集后可在离线脚本中导出 Ragas 数据，不让评测依赖影响阅读工作台。 |

## 高价值候选项目与适用场景

下面这些项目是“遇到明确边界再接入”的候选，不是运行时必装依赖。它们解决的是不同问题，不能用一个 Agent 框架替代检索服务、任务队列和证据库。链接指向项目官方文档或仓库，选型时优先检查许可证、Python 版本、数据外发方式和持久化能力。

| 项目 | 最适合的 LitLoom 场景 | 适配方式与边界 |
|---|---|---|
| [LlamaIndex](https://docs.llamaindex.ai/) | 论文库接入外部知识源、节点化文档索引、引用回溯和知识图谱实验 | 可作为离线索引实验或连接器层；页码、文本哈希和原文仍由 LitLoom 保存，不能让框架生成的节点 ID取代本地证据锚点。 |
| [Haystack](https://docs.haystack.deepset.ai/) | 可组合的检索、过滤、重排序和问答流水线，对比 BM25/向量/混合策略 | 适合作为可选 RAG provider 做离线对照；在线结果必须回写来源、页码、检索方法和评分，不能绕过现有权限。 |
| [DSPy](https://dspy.ai/) | 用固定标注集优化检索规划、初筛和论文综合提示 | 只用于离线 prompt/模块优化；优化结果需要通过 LitLoom 评测门禁，禁止在线自动改写提示并影响历史可复现性。 |
| [Microsoft AutoGen](https://microsoft.github.io/autogen/) | 多角色讨论、审查者与汇总者协作的实验性工作流 | 适合研究型原型或人工可见的审查回合；不能替换 SQLite DAG 的恢复、预算和人工审批状态。 |
| [CrewAI](https://docs.crewai.com/) | 轻量的角色分工演示，例如检索员、证据核验员、批评员 | 只作为导入/导出工作流的实验适配器；角色输出必须经过统一契约和证据质量门，不能直接写库。 |
| [Temporal](https://temporal.io/) | 团队部署中的长任务、定时追踪、重试和跨进程恢复 | 当任务量超过本地线程与 SQLite 边界时，可作为外部执行器；`agent_runs`/`agent_steps` 仍是业务状态，必须支持幂等重放。 |
| [Prefect](https://docs.prefect.io/) | 研究组内部的调度、队列、失败重试和运行观测 | 可接管定期前沿追踪和批量 PDF 预处理；不负责论文身份、证据引用或权限，外部调度只提交受限任务。 |
| [Browser Use](https://github.com/browser-use/browser-use) | 需要登录后人工确认的出版社页面、网页剪藏和可视化浏览流程 | 只允许在明确授权的浏览器沙箱使用；默认仍优先官方 API/Tavily，禁止自动绕过验证码、付费墙或批量抓取。 |
| [OpenHands](https://github.com/All-Hands-AI/OpenHands) | 维护脚本、生成迁移、运行质量门等软件工程型辅助任务 | 与论文数据隔离，使用临时工作区和只读默认权限；不让代码 Agent 直接访问模型密钥、PDF 私有目录或生产数据库。 |
| [Instructor](https://python.useinstructor.com/) / [Outlines](https://dottxt-ai.github.io/outlines/) | 结构化模型输出、枚举约束和 JSON 修复实验 | 可作为模型边界的可选实现；当前内置契约校验保持零依赖和可审计，第三方解析失败要回退到统一重试与错误记录。 |
| [Apache AGE](https://age.apache.org/) | 团队部署中保存引用图、版本关系和研究主题图 | 仅在 SQLite 图关系成为瓶颈时引入；当前 `citation_edges` 与 `paper_version_links` 已足够，不能为图数据库牺牲离线备份。 |

### 按应用场景的推荐组合

- **个人多年使用**：官方学术 API + Semantic Scholar/OpenAlex 推荐 + LitLoom 本地 DAG/RAG + MCP。无需额外 Agent 框架，维护面最小。
- **论文证据问答**：PDF 页级解析 + BM25/Embedding 混合检索；需要做算法对照时再用 Haystack 或 LlamaIndex，原文证据仍回到 LitLoom。
- **检索质量优化**：固定人工标注集 + DSPy 离线优化 + Ragas 离线评测；每次变更保留 `score_version`、提示版本和指标基线。
- **研究组多人运行**：PostgreSQL/worker 边界 + Temporal 或 Prefect 调度；MCP/HTTP 入口继续走角色、租户、人工确认和审计。
- **网页剪藏**：Tavily/API 优先；只有用户明确授权且页面确实需要交互时，才启用 Browser Use，并将网页内容标为外部线索，不能自动变成论文证据。
- **工程维护**：OpenHands 只在隔离工作区生成代码、测试和迁移草案，必须通过质量门和人工审查后合并。

## LitLoom 的接入规则

1. 外部学术搜索和推荐优先使用来源官方 API 或维护稳定的学术服务。OpenAlex、Crossref、arXiv、Europe PMC、Semantic Scholar、DBLP、OpenReview 和可选 Tavily 是召回工具；研究画像推荐并行使用 OpenAlex related works 与 Semantic Scholar hosted recommendations，本地代码只负责规范化、强标识去重、意图相关性门禁、来源引用和用户反馈排序。Tavily 未核验网页只能作为线索，不能自动入库。
2. 模型层保持 OpenAI-compatible 契约。模型路由只选择任务模型，不把供应商 SDK 写入论文域逻辑；如果需要 LiteLLM，放在端点外部或可选 worker 中，保证切换网关不改变 `ai_artifacts`、输入哈希和任务状态。
3. Agent 编排的持久状态以 LitLoom 的 `research_goals`、`agent_runs`、`agent_steps` 和 `agent_tool_calls` 为准。任何外部编排库都必须能恢复这些记录、保留 `awaiting_confirmation`，并且不能把失败步骤伪装成成功。工具边界会校验已提供的输入类型与步骤输出契约（含嵌套字段、必填字段、允许值和数组下限）；校验失败会写入工具调用错误并进入有限重试。
4. 工具按 Read、Draft、Apply、External 分级。文件外发、公开 PDF 下载、写入阅读卡、导出报告等操作必须经过明确确认；MCP 服务令牌绑定团队成员后仍执行同样的角色检查和审计。
5. RAG 的原始证据永远来自本地 PDF 页文本或页面图像，向量只是排序信号。每个证据必须保留附件、页码、字符偏移或视觉页引用；Embedding 不可用时回退 BM25，并把回退原因展示给调用者。

## 引入前的验收门槛

- 不改变现有 HTTP/OpenAPI 和 SQLite 导入导出；旧数据库可以启动、备份和恢复。
- 运行时依赖必须有明确的可选 extra，默认 `python app.py` 仍能在 Python 3.10 无第三方包启动。
- 先用 fixture 测试结构化输出、重试、取消、断点恢复、权限和审计，再接真实模型或公网服务。
- 任何搜索/推荐组件都要记录来源、版本、请求时间、限流和失败原因；不能只返回“没有结果”。
- 任何 RAG/Agent 质量改动都要在固定人工标注集上比较 recall@k、MRR、Precision@k、引用覆盖和任务完成率，并保留旧版本基线。

因此，当前版本选择“官方学术 API + 可选模型中转 + 持久化本地编排 + MCP 工具边界”。只有当并发、部署形态或离线评测确实超过现有实现的边界时，才接入上述成熟项目的对应部分。
