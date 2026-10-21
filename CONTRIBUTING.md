# LitLoom 企业级协作约定

LitLoom 当前采用“模块化单体 + PostgreSQL + 持久化后台任务”。这是一种适合个人长期使用、实验室内部部署和逐步扩展的企业工程形态：领域模块边界清楚，数据库和任务状态可恢复，未来需要拆分服务时再沿端口和契约拆分，不为了形式提前引入微服务复杂度。

## 一次变更的标准流程

1. **需求和验收**：先写出用户可观察的行为、边界条件和指标。检索质量使用 Precision@5/10、重复率和反馈覆盖率；任务性能使用 p50/p95、失败率和恢复时间。
2. **影响分析**：确认涉及的领域模块、数据库表、API 契约、前端状态和运维风险。跨模块设计决策写入 `ADR.md`。
3. **实现**：依赖方向从 `web -> application/domain -> infrastructure`，业务模块不直接解析环境变量、不拼接 HTTP 响应、不读取密钥。兼容入口 `app.py` 只负责启动和导出旧 API。
4. **测试**：为领域规则写快速单元测试；为 HTTP 契约写集成测试；为数据库变化写迁移测试。模型和外部网站默认使用 stub，不把真实 API 调用放进 CI。
5. **验证**：执行 `.scripts\verify.ps1`。需要排查环境时执行 `.scripts\doctor.ps1`；它是只读的，不会启动服务或调用模型。
6. **发布**：先备份 PostgreSQL 和附件，执行迁移，做健康检查和 smoke test，再切换版本。所有发布必须能回滚到上一个应用版本和数据库备份。
7. **复盘**：记录实际 p95、错误码、外部来源失败和用户反馈；把修复转为测试或监控，避免同类问题重复出现。

## 代码边界

- `literature_desk/web/`：HTTP 路由、认证、序列化和状态码；不写检索算法。
- `literature_desk/discovery.py`、`library.py`、`paper_analysis.py`：论文领域用例；通过 `services` 获取基础设施兼容能力。
- `literature_desk/documents.py`、`postgres.py`、`providers.py`：外部系统和文件/数据库适配器；必须有超时、重试边界和可观测错误。
- `literature_desk/jobs.py`：持久化任务生命周期、取消、恢复和并发预算；任务必须幂等或明确记录不可重试原因。
- `literature_desk/config.py`：唯一的运行配置诊断入口。新配置先加入 `.env.example`、配置快照和文档，再被业务使用。
- `literature_desk/errors.py`：稳定错误码和可重试语义。客户端依赖 `error_code`，展示文本仍保留在 `error`。
- `app.py`：启动组合根和历史兼容门面。不要在其中新增业务规则。

## 提交前检查

```powershell
.\scripts\doctor.ps1
.\scripts\verify.ps1
```

提交信息使用动词开头并说明范围，例如 `fix(discovery): persist screening progress`。不要提交 `.env`、API Key、PDF 原件、数据库文件和运行日志。Pull Request 至少说明行为变化、迁移影响、测试命令、监控指标和回滚方式。
