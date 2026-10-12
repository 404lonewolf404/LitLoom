# 研读工作台运维与发布手册

这份手册把本地个人部署按一个小型生产服务管理。它不要求云基础设施，但要求每次发布都能说明数据完整性、检索质量和运行状态。

## 启动与探活

```powershell
cd "C:\Users\lonewolf\Desktop\多模态\研究生论文工作台"
python .\app.py
Invoke-RestMethod http://127.0.0.1:8765/api/ready
Invoke-RestMethod http://127.0.0.1:8765/api/health
Invoke-RestMethod http://127.0.0.1:8765/api/diagnostics
```

`/api/ready` 适合进程管理器；`/api/health` 检查 SQLite 完整性、schema 和调度器；`/api/diagnostics` 用于版本发布检查和问题定位。三个接口都不会返回 API Key、论文笔记、请求体或 PDF 内容。

长期运行时可额外检查 `GET /api/maintenance/status`：论文数应与全文索引文档数一致，`orphans` 应为 0；索引异常时调用 `POST /api/maintenance/rebuild-search-index`。`GET /api/sync-jobs` 可核对运行阶段、重试时间和服务重启造成的 interrupted 作业。

应用默认每 24 小时自动创建一次完整归档，设置页或 `GET/PATCH /api/settings/backup-policy` 可调整间隔、保留份数和暂停状态。自动归档以 `researchdesk-auto-` 开头并按保留数量轮换；手动归档以 `researchdesk-backup-` 开头，始终保留。应用关闭期间不会执行任务，生产性长期使用应把 `data/backups/` 复制到另一存储位置。

## 发布门禁

每次修改检索、数据库、模型配置或前端后执行：

```powershell
.\scripts\verify.ps1
```

门禁包含 Python 编译、浏览器 JavaScript 语法和单元/HTTP 集成测试。发布前还应检查：

1. `GET /api/health` 的 `database.integrity` 为 `ok`，论文数量与发布前一致。
2. `GET /api/diagnostics` 的 schema 版本与迁移代码一致，`missing_abstract_pct`、`duplicate_title_year_groups` 和反馈覆盖率有解释。
3. `GET /api/analytics` 的 `retrieval_funnel`、`data_quality`、`development_cycle` 和 `optimization_actions` 能说明当前观察窗口、详情完整度、质量门禁和下一步动作；没有反馈样本时必须显示“样本不足”，不能把零样本当作质量提升。
4. `GET /api/metrics` 的 p95 延迟低于 1500 ms，5xx 低于 1%。首次启动样本不足时记录为观察状态，不把零样本当成性能结论。
5. 创建一次 ZIP 备份并检查 manifest、SQLite SHA-256 和附件清单；确认自动备份策略状态可读。
6. 打开“发现论文”和“系统设置”页面，确认 API 返回 200、OpenAI 配置不会回显密钥。

## 备份与恢复

备份前先停止大批量同步，页面“创建完整备份”会生成一致性 SQLite 快照、附件副本和 manifest。恢复操作会先在本地留下当前数据库副本，再校验 ZIP 的 schema、数据库哈希和附件哈希。恢复后重新执行 `/api/health`、`/api/diagnostics` 和论文数量核对。

API Key 不属于备份资产。恢复后本地密钥文件不会被 ZIP 覆盖；如果迁移到另一台机器，需要单独复制受保护的 `model-secret.json`，或通过环境变量/设置页重新配置。

## 检索质量复盘

每周查看“数据分析”页面：

- 积压增长而 Precision@5 稳定：降低主题同步频率或缩小查询范围。
- 相关反馈覆盖率不足：先做人工分诊，再调整规则或接入 embedding；不要用无标注样本宣称模型变好。
- 去重率异常升高：检查来源重复、版本标识和查询变体；保留 source record，禁止直接删除原始观察。
- 某来源失败率升高：暂停该来源或降低频率，确认其他来源成功后再判断是否真的没有新论文。
- p95 升高但数据库完整：优先检查外部连接器超时、变体数量和 PDF 派生任务，不把模型调用和核心 SQLite 事务混在一起。

所有评分调整都应保留 `score_version`、来源查询路径和原始反馈，使用同一批判断比较 Precision@5/10。

## 故障处理

`/api/health` 为 `degraded` 时先停止调度，保留最近备份和 `/api/audit` 输出。来源 4xx/5xx 只影响该来源，重试计划会写入主题记录；不要为了“让页面有结果”删除失败记录。模型服务不可用时，论文发现、去重、笔记、导出和备份仍应可用。

## 演进边界

当前是本地模块化单体，SQLite WAL 适合单用户多年资料。数据量、并发或协作需求显著增长时，先沿 OpenAPI 契约抽出 connector worker，再迁移 PostgreSQL；迁移必须保持 `papers`、`source_records`、`query_matches`、`audit_events` 和备份恢复语义不变。
