# LitLoom 的 MCP 适配器

LitLoom 的网页端内部继续使用 OpenAI 兼容的 Function Calling。除此之外，项目现在提供一个可选的 Model Context Protocol（MCP）stdio 适配器，让 Claude Desktop、Cursor、Codex 或其他 MCP 客户端把 LitLoom 当作长期论文工具使用。

适配器是一个无第三方依赖的轻量进程，复用已经运行的 LitLoom HTTP 服务和 SQLite 持久化任务。它不会读取模型 API Key，也不会把 Key 放进 MCP 响应或日志。搜索始终先进入候选审阅区；导入文献库仍需要在 LitLoom 中明确确认。

如果 LitLoom 开启了 `LITLOOM_AUTH_MODE=team`，为 MCP 单独配置一个本地服务令牌：在受保护文件中写入随机高熵字符串，并设置 `LITLOOM_MCP_TOKEN_FILE` 与 `LITLOOM_MCP_USERNAME`。MCP 请求会以该成员身份执行，继续受到其角色限制并写入审计；令牌不进入 URL、数据库、模型提示词或日志。未配置令牌或用户名时，团队认证不会被绕过。

## 启动

先启动 LitLoom（默认端口 8772 的实例）：

```powershell
cd "C:\Users\lonewolf\Desktop\多模态\研究生论文工作台"
$env:LITLOOM_URL = "http://127.0.0.1:8772"
python .\mcp_server.py
```

安装项目后也可以使用 `litloom-mcp` 命令。MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "litloom": {
      "command": "python",
      "args": ["C:\\Users\\lonewolf\\Desktop\\多模态\\研究生论文工作台\\mcp_server.py"],
      "env": {
        "LITLOOM_URL": "http://127.0.0.1:8772"
      }
    }
  }
}
```

## 工具边界

- `litloom_search_papers`：启动可恢复检索。支持模型自主检索（`model_web`，会沿用 DeepSeek/Tavily 配置）或指定学术连接器；返回 `run_id`，结果用 `litloom_search_status` 查询。状态工具按审阅所需字段压缩候选，并支持 `offset`/`limit` 分页，避免候选摘要过大时整批结果被 MCP 传输上限截掉。
- `litloom_fetch_web_page`：使用已配置的 DeepSeek Fetch 读取一个公开网页的 Markdown 正文。只返回有来源标识的外部网页证据，不会写入文献库；本机和私网地址会被拒绝。
- `litloom_browse_web`：使用 Tavily Extract 批量读取最多三个公开网页；与 DeepSeek Fetch 分开配置和标识来源。
- `litloom_search_history`：读取历史问题和候选，避免多年使用时重复检索。
- `litloom_get_recommendations`：读取本地研究画像以及 OpenAlex、Semantic Scholar 官方推荐服务的候选；外部候选仍需在 LitLoom 中明确确认后才入库。
- `litloom_search_library`、`litloom_get_paper`、`litloom_search_document`：读取馆藏、论文详情和逐页 PDF 证据。逐页工具可传 `semantic=true` 请求 Embedding + BM25 混合检索，结果带 `retrieval_mode` 和明确的 BM25 回退原因。
- `litloom_semantic_index_status`、`litloom_build_semantic_index`：检查页级索引或异步建立索引。建立操作必须传 `consent=true`；它会把该 PDF 的提取文本发往配置的 Embedding 服务，用户必须先明确同意。任务可通过网页 API 的 job ID 查看和取消。
- `litloom_build_paper_overview`：为已入库单篇论文生成专业证据总览，覆盖方法、实验、局限、复现缺口及后续行动。只读取本地保存内容，不调用模型或下载 PDF。
- `litloom_start_analysis`：启动摘要助读、快速阅读卡、读前准备或多模态精读。必须传 `consent: true`；任务在刷新、切页和服务重启后继续。
- `litloom_get_ai_job`、`litloom_retry_ai_job`：查看持久化进度、错误、部分结果并重试。
- `litloom_health`：检查本地服务和数据库状态。

MCP 适配器本身不绕过 LitLoom 的候选审阅、用户同意、任务重试和持久化规则。网页端仍然可以独立运行；没有 MCP 客户端或没有额外 Python 包时，主程序不会受到影响。
