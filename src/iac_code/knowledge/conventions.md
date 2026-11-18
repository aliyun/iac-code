# 知识维护约定

新增页面先搜索已有知识。通用原理放 common/；同一产品的概览、专题、API 与支持条件统一放 clouds/<厂商>/products/<产品>/，目录内先平铺文件，产品主文档保留常用知识与相关链接。内容较长或独立时再拆页，不预建功能子目录；跨产品知识选定一个主归属，由其他产品引用，不复制正文。具体实体按所属技术领域归类。

页面使用纯 Markdown，以一级标题开头；正文写清事实、适用条件和必要引用。产品归属由目录表达。来源与实际核验日期集中保存在知识目录外的已有迁移校验清单中，未经核验不生成日期。

新增知识更新必要的局部导航和交叉引用，根导航不列全量条目。不得将聊天纠正或单地域查询自动写成全局事实；冲突保留来源和范围。模板推进、授权与 complete_step 协议仍由 Skill 和代码负责。

知识库内部链接统一使用 `knowledge://` 根路径，例如 `knowledge://common/hardware/confidential-computing.md`；直接作为 read_file、grep、glob 的 path，不使用 `../` 拼接，也不经 Bash 或 web_fetch 打开。

共享模板知识统一使用 `knowledge://clouds/aliyun/products/ros/` 引用。Skill 保留执行流程与脚本。
