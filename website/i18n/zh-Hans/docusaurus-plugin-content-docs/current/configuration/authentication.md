---
title: 认证
description: 使用认证流程配置 LLM 和云凭证。
---

# 认证

在终端运行 `iac-code`，即可启动本地 Web 应用并打开浏览器。首次使用时，在设置中配置 LLM 提供商和阿里云凭证。

- **设置 → 模型**：选择提供商，填写 API Key，并选择模型。
- **设置 → 云凭证**：选择阿里云、认证方式和地域。

Web、桌面版和 headless 共用这些已保存的凭证。

使用终端配置时，启动 `iac-code tui` 后运行 `/auth`。

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
