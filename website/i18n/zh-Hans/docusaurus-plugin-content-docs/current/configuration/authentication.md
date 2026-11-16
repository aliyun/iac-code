---
title: 认证
description: 使用认证流程配置 LLM 和云凭证。
---

# 认证

在终端运行 `iac-code`，即可启动本地 Web 应用并打开浏览器。首次使用时，在设置中配置 LLM 提供商和阿里云凭证。

终端界面保留为 `iac-code tui`，但已停止更新和维护。后续使用建议选择 Web 版或桌面版。

在交互模式中使用 `/auth` 配置模型提供商访问和阿里云访问。

```bash
iac-code tui
```

```text
/auth
```

认证流程会引导你选择提供商、输入 API Key、选择模型并设置阿里云凭证。

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
