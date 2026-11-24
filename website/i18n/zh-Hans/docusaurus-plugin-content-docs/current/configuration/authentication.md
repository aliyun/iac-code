---
title: 认证
description: 在桌面版或 Web 的设置中配置模型和云访问。
---

# 认证

打开桌面版，或在交互终端运行 `iac-code` 打开 Web，然后在设置中配置模型和所需的云凭证。

- [在桌面版或 Web 中配置模型](./llm-providers.md)
- [在桌面版或 Web 中配置云访问](./alibaba-cloud-credentials.md)

Web、桌面版和 headless 共用这些已保存的凭证。

运行时配置存储在用户配置目录下：

```text
~/.iac-code/
```

重要文件包括：

| 文件 | 用途 |
|---|---|
| `.credentials.yml` | LLM 提供商凭证 |
| `.cloud-credentials.yml` | 阿里云凭证 |
| `settings.yml` | 运行时设置 |
