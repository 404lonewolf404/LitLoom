# Interview Demo Runbook

这是一条约 8 分钟的可复现演示路径，适合实习项目答辩或 code review。

## 1. 启动与健康

```powershell
python .\app.py
Invoke-RestMethod http://127.0.0.1:8765/api/health
```

先讲清楚：服务只绑定本机回环地址，SQLite 使用 WAL，论文库、PDF 和备份都在本机。

## 2. 展示模型配置

打开“系统设置 → 打开模型配置”，展示 OpenAI-compatible 表单：

- `Base URL` (optional): the provider service root, for example `https://relay.example.com/v1`
- `API 请求地址` (optional): a complete POST endpoint; when present it is used verbatim
- `Model`: `gpt-4.1-mini`
- `Temperature`: `0.2`
- API Key 输入框不会回显，数据库没有 `api_key` 列；默认会保存到本地 `model-secret.json`，设置页能显示当前来源

两者至少填写一个。页面的“测试连接”会直接调用真实推理端点并解析 JSON/SSE；不要求服务提供 `/models`。没有 Key 时，核心检索仍然可用，AI 功能明确降级。

## 3. 展示检索智能

在“发现论文”输入 `多模态推理`：

1. 选择 arXiv、OpenAlex、Crossref，必要时开启 OpenReview。
2. 选择“深度检索”和“邻域探索”。
3. 展示检索计划中的中英文桥接、综述/基准/失败分析/前沿入口。
4. 展示结果的高相关、邻近方向、跨领域线索、来源数量、命中路径和重复状态。
5. 点击“AI 拓展检索”，说明 AI 只提出待确认检索表达，不自动修改数据库。

## 4. 展示长期记忆

保存一个主题追踪，运行两次，标记一篇相关、忽略一篇。第二次运行展示 `seen_before`、`seen_count`、主题级忽略和只看未重复选项。强调同一篇论文可以在不同主题中保持独立判断。

## 5. 展示阅读沉淀与批量分诊

在文献库勾选多篇论文，批量加入待读或标记已读；打开一篇论文，在阅读台填写结构化阅读卡并保存，展示完成率进入数据分析。自由笔记和可核验证据字段分开保存，适合长期复盘。

## 6. 展示数据驱动开发

打开“数据分析”，讲解四层证据：

- 检索漏斗：原始抓取 → 过滤 → 候选 → 新增
- 质量：反馈覆盖率、Precision@5/10、去重率、候选接受率
- 可靠性：来源成功率、部分失败、平均同步耗时
- SLO：请求 p50/p95、4xx/5xx 和数据库完整性

在自动追踪页点击一次运行的“查看详情”，展开来源健康、过滤计数和查询变体命中；历史旧运行会显示详情缺失，不会伪造数字。

然后展示“开发优化闭环”中的 P1/P2 动作，说明系统会依据实际数据建议下一步，而不是凭感觉调参。

## 7. 展示可迁移与工程质量

调用 `/api/diagnostics`、`/api/audit` 和 `/api/export/json`，再创建一次完整备份。最后运行：

```powershell
.\scripts\verify.ps1
python -m build --wheel --no-isolation
```

结论应围绕可解释、可审计、可回滚、可迁移和模型可替换展开。
