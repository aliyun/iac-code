## 六资源四槽滚动验证

本文件定义六资源四槽验证的通用验收要求，不选择当前运行。实际冻结计划以用户当前 Goal 和程序 `status.active_plans` / `next.active_plans` 为准；恢复或上下文压缩后不得把旧轮次范围套入新任务。

历史首轮 rolling-six-20261006：R0009 VPC、R0015 LaunchTemplate、R0016 NetworkInterface、R0017 PrefixList、R0018 SecurityGroup、R0034 AccessGroup。它是已执行计划的定位信息，不是持续调度指令；已有 R0001 和历史 PASS 不重放。后续轮次保存独立冻结 plan_id，不改历史计划。

每资源 ROS、ROS 托管 Terraform、创建 API、查询 API 四维度；旧导航 PASS 保留，未验创建链仍须创建/清理。新增写操作必须由 OPS 核对实际 packet 后授权，预算 0，不创建付费计算、存储、EIP 等依赖。

唯一 Luna / high、唯一 Sol / high、每资源一个 GLM / xhigh。四个独立 Web 16100–16103/cwd/config/session，共用一个专用 Chrome 登录态，target 精确隔离，短 UI 锁串行；等待产品回答时不占整回合浏览器锁。

首波使用 initial_wave=4，由程序持久状态控制，凑齐四份合法 packet 才派发，后两项滚动补位。授权、费用或输入阻塞必须报告，不通过减少首波门槛掩盖。

验收须分别证明：六资源精确缺项原始来源/截图和清理；四个真实 GLM 回合存在同时间重叠；第五和第六项在独立验收/清理释放槽后复用槽；唯一 Luna 的实际 active goal 持续自动 next/execute，不依赖定时任务；重复 continuation 不重复提交，修复/恢复不丢状态。仅四槽配置或离线测试不能记并发成功。以原始 goal/turn/事件时间及数据库绑定为准，最终写出验证结果。

全部结束后生成现有 resource-link-results.html，不扩大到第七项；finished 与全通过不同。新增权限/付费或计费未知需用户决定。
