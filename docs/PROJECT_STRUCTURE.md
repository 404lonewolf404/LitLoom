# LitLoom 项目结构

## 分层关系

```text
浏览器 / MCP 客户端
        |
        v
literature_desk/web       HTTP、认证、错误契约
        |
        v
领域用例模块              discovery / library / paper_analysis / jobs
        |
        v
适配器和基础设施           providers / documents / postgres / model_runtime
        |
        v
PostgreSQL、文件系统、外部学术索引、模型网关
```

这是模块化单体，不是把每个目录当成独立服务。模块通过稳定函数和数据库表协作；后台任务通过持久化状态恢复，浏览器刷新不会丢失任务。

## 关键目录

| 路径 | 职责 | 变更时要同步 |
| --- | --- | --- |
| `app.py` | 启动、依赖组合、旧 API 兼容导出 | 启动脚本、健康检查 |
| `literature_desk/config.py` | 环境配置、校验、脱敏诊断 | `.env.example`、`CONTRIBUTING.md` |
| `literature_desk/errors.py` | 错误码、HTTP 可重试语义 | OpenAPI、前端错误提示 |
| `literature_desk/web/` | 路由和 HTTP 适配 | `openapi.yaml`、集成测试 |
| `literature_desk/migrations/` | PostgreSQL/SQLite 演进脚本 | 迁移测试、发布清单 |
| `literature_desk/jobs.py` | AI/PDF/检索任务状态机 | 恢复、取消、并发测试 |
| `static/js/` | 按页面拆分的浏览器行为 | 浏览器 smoke test |
| `tests/` | 单元、契约、集成和后端兼容测试 | CI 质量门禁 |
| `scripts/` | 本地诊断、验证、迁移和评估入口 | CI、运维手册 |

## 依赖规则

1. HTTP 层可以调用领域用例，领域用例不能反向调用 HTTP Handler。
2. 领域模块通过 `literature_desk.services` 访问应用组合根中的运行时依赖；新代码优先使用显式参数和小型端口。
3. 配置只从 `config.py` 读取并在启动诊断中脱敏展示；任何日志都禁止输出密钥、Authorization、PDF 正文和用户笔记。
4. 数据库结构变化必须新增迁移，不允许在业务请求中临时 `ALTER TABLE`。SQLite 兼容初始化只用于迁移来源和测试。
5. 外部请求必须有连接/读取超时、有限重试、错误分类和来源级降级；不得在 HTTP 请求线程中执行无界全文分析。
