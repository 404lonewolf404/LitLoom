# Development and Release Workflow

这份手册描述一次可追溯的工程迭代。目标是让“改了检索策略”能够回答三个问题：影响了什么、数据是否证明变好、如何回滚。

## 1. 需求与验收

先把需求写成行为和指标：例如“减少长期主题的重复结果”对应 `seen_before`、`query_matches.seen_count`、`suppressed_count` 和重复率；“提高排序质量”对应明确的相关/忽略样本和 Precision@5/10。不要用页面截图替代验收条件。

多人平台和全文论文分析的目标代码边界、迁移顺序、租户隔离测试与证据质量门槛见 [`MULTIUSER_PLATFORM_DESIGN.md`](MULTIUSER_PLATFORM_DESIGN.md)。在阶段 A 完成模块化和契约测试前，不要直接把真实 SQLite 数据库改造成多人共享库。

当前代码按领域放在 `literature_desk/`：`workspaces.py` 和 `database.py` 管理身份/持久化边界，`library.py` 管理文献记录，`documents.py` 与 `paper_analysis.py` 管理 PDF 和阅读分析，`providers.py` 与 `discovery.py` 管理来源检索及候选编排，`jobs.py` 管理后台任务，`research_agents.py` 管理目标工作流，`web/handler.py` 管理 HTTP 适配。`app.py` 只保留运行时配置、启动逻辑与旧 API 兼容导出。不要让领域模块反向 `import app`；历史依赖暂经 `literature_desk.services` 兼容绑定，新增代码优先通过函数参数或小型端口传递依赖。

企业工程边界、项目目录和发布决策分别见 [`CONTRIBUTING.md`](CONTRIBUTING.md)、[`docs/PROJECT_STRUCTURE.md`](docs/PROJECT_STRUCTURE.md) 和 [`docs/ENGINEERING_PLAYBOOK.md`](docs/ENGINEERING_PLAYBOOK.md)。配置诊断使用 `scripts/doctor.ps1`；API 错误统一保留 `error`，并提供 `error_code`、`retryable`、`request_id`，契约见 `openapi.yaml` 的 `ErrorResponse`。

## 2. Baseline 与实现

1. 记录当前版本、schema、论文数和最近备份。
2. 只修改一个主要变量，例如查询变体数量、来源权重或过滤阈值。
3. 给新的评分/数据源写来源路径、错误处理和回退行为。
4. 保留人工字段、原始 source record 和审计事件，不静默覆盖笔记。

## 3. 自动检查

```powershell
.\scripts\verify.ps1
python -m build --wheel --no-isolation
```

质量门禁覆盖 Python 编译、JavaScript 语法、单元测试、HTTP 集成测试、备份恢复和敏感信息边界。GitHub Actions 在 push 和 pull request 上执行同一门禁。

## 4. 运行时检查

发布后检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health
Invoke-RestMethod http://127.0.0.1:8765/api/diagnostics
Invoke-RestMethod http://127.0.0.1:8765/api/analytics
Invoke-RestMethod http://127.0.0.1:8765/api/metrics
```

确认版本、schema、论文总数、数据库完整性、调度器、来源健康、检索漏斗和 SLO。发布后若服务进程没有重启，页面可能仍使用旧的 Python 模块；必须核对 `/api/health.app_version` 和 `process_started_at`。

## 5. 质量复盘

每周按以下顺序看数据：

| 信号 | 可能原因 | 下一步 |
|---|---|---|
| Precision@5 下降 | 查询扩展过宽、来源噪声增加 | 检查命中路径和排除词，比较 `rankfusion-v2` |
| 去重率上升 | 多个来源或变体返回同一批论文 | 降低重复变体，保留强标识和版本信息 |
| 接受率过低 | 过滤条件过严或来源元数据缺失 | 拆分来源查看过滤量，放宽单个条件 |
| 某来源失败 | 限流、网络挑战或 API 变更 | 独立重试/暂停该来源，不宣布全网无新论文 |
| p95 上升 | 外部连接器等待、变体过多或 PDF 任务 | 限制超时和变体预算，异步化重任务 |
| 反馈样本不足 | 用户没有完成分诊 | 先补人工标注，再评估新排序 |

## 6. 回滚

发布前创建 ZIP 备份。若新版本导致结果或迁移异常：停止调度、保留 `/api/audit` 和 `/api/diagnostics`、恢复安全副本，核对论文数和附件哈希，再回滚代码版本。评分版本变化不能删除旧的 query match 记录。

## 7. 面试可讲的证据

- 需求：`需求分析.md`
- 架构取舍：`ADR.md`、`ARCHITECTURE.md`
- 指标与动作：`/api/analytics`、数据分析页、`OPERATIONS.md`
- 质量：`tests/test_app.py`、`scripts/verify.ps1`、`.github/workflows/quality.yml`
- 发布：`CHANGELOG.md`、wheel 构建、健康检查和备份恢复
