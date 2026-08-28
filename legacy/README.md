# legacy/ — 历史版本存档

本目录存放 **V1 时代**的代码，仅作历史参考，**不参与任何运行路径**：

| 文件 | 说明 | 为何归档 |
|---|---|---|
| `graph_v1.py` | V1 的 StateGraph 版本（route → retrieve → human → generate → reflect） | V2 已改为 `create_react_agent` 通用 Agent（见 `graph.py`），StateGraph 节点不再被引用 |
| `llm.py` | V1 的模型封装（`build_llm` / `_OpenAICompatLLM` 等） | 只被 `graph_v1.py` 引用；V2 统一走 `graph.py` 的 `build_agent`（基于 `ChatOpenAI`） |

**为什么保留而不是删除**：V1 的 StateGraph 写法（显式节点、条件边、human-in-the-loop 占位）
对想学习 LangGraph 底层 API 的读者仍有参考价值，故留档。但请不要在 V2 中 `import` 它们。

## 已知问题（归档原因）

- `llm.py` 的 `build_llm` 只支持 4 个 provider，与 `config.py` 声明的 6 个不一致（缺 qwen/zhipu/moonshot）——两套封装并存曾导致行为不一致
- `graph_v1.py` 的 `reflect` 节点用"回答中是否含片段单词"判断 grounded，过于粗糙
