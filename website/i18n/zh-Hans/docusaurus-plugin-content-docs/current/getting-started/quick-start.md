---
title: 快速开始
description: 启动桌面版或 Web，完成设置并发送第一条请求。
---

# 快速开始

以下步骤同时适用于桌面版和 Web，两者使用同一套对话界面。

1. 打开[桌面应用](../desktop-app.md)，或在交互终端运行 `iac-code` 打开 Web。

2. 单击**新对话**，在输入框下方的项目控件中选择模板保存目录。

3. 按[模型配置](../configuration/llm-providers.md)的步骤保存提供商配置并设为当前模型；需要云查询或部署时，再配置[云凭证](../configuration/alibaba-cloud-credentials.md)。

4. 返回输入框，选择**普通模式**，发送：“生成一个 VPC ROS 模板，保存到 templates/vpc.yml，暂不部署。”

5. 在对话中查看处理过程。若执行了 Wiki 搜索，展开对应条目可查看关键词和命中内容；出现文件写入权限卡片时，核对操作并作出选择。

6. 助手写入模板后，在**输出**区域单击文件名打开预览，再通过后续提问解释或校验参数。对话会保存在侧栏中。

需要按步骤规划和部署时，新建对话并选择[流水线模式](../automation/pipeline-mode.md)。日常操作见[常用操作](../cli/commands.md)，脚本场景见[非交互自动化](../automation/non-interactive-mode.md)。
