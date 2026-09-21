# 部署与运行

服务默认监听 **127.0.0.1:8000**，只绑定回环地址，不对外暴露。启动方式为
`python -X utf8 main.py web`，其中 `-X utf8` 用于规避 Windows 命令行下的中文
乱码问题。也可以用 `--host` 与 `--port` 显式指定监听地址与端口。

模型服务商通过环境变量配置。**LLM_PROVIDER** 选择服务商（deepseek、openai、
qwen、zhipu、moonshot、ollama），对应的 API Key 由各服务商专属变量提供，
例如 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。**LLM_MODEL** 可以覆盖默认模型名。

**LLM_BASE_URL** 用于指向任意 OpenAI 兼容网关，例如 one-api、vLLM 或自建代理。
它的优先级高于服务商预设地址，因此自建网关可以完全接管出网流量，不需要改动
代码。

**API_TOKEN** 是可选的访问令牌。设置后，消耗额度或写入配置的接口会要求请求头
携带 X-API-Token；未设置时保持开放，适用于纯本机使用场景。

**容器化部署**提供了 Dockerfile。镜像以非 root 用户运行，端口默认只映射到回环
地址，环境变量通过 --env-file 注入而绝不复制进镜像。构建时可以传入
INSTALL_EMBEDDING=0 跳过沉重的语义检索依赖，此时检索自动回退纯 BM25。

**日志目录**由 LOG_DIR 控制，未设置时默认写入项目下的 logs 目录。审计日志
与普通应用日志分离存放，且都采用按大小轮转的策略，避免无限增长撑爆磁盘。

**索引构建**是可选的独立步骤：`python main.py build`。文档问答以外的功能
（普通对话、联网搜索、天气、天气之外的工具调用）不需要索引即可使用。
