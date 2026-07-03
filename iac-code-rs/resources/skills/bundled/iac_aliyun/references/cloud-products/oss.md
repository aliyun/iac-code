# 阿里云 OSS 对象存储指南

## 存储类型

| 类型 | 访问频率 | 最低存储时间 | 适用场景 |
|------|----------|-------------|----------|
| 标准（Standard） | 频繁 | 无 | 热数据、网站图片、视频 |
| 低频访问（IA） | 不频繁（月均 1-2 次） | 30 天 | 备份、归档数据 |
| 归档（Archive） | 极少 | 60 天 | 长期归档，读取需解冻 |
| 冷归档（Cold Archive） | 极少 | 180 天 | 合规存档，成本最低 |

**推荐**：业务数据用标准，日志/备份用低频访问，合规数据用归档。

## Bucket 配置要点

资源类型 `ALIYUN::OSS::Bucket`，关键属性：
- `StorageClass`：Standard / IA / Archive / ColdArchive
- `AccessControl`：private（推荐）/ public-read / public-read-write
- `VersioningConfiguration.Status: Enabled`：开启版本控制，防误删
- `ServerSideEncryptionRule.SSEAlgorithm: AES256`：服务端加密

## 访问控制

### 访问权限级别

| 权限 | 说明 | 推荐场景 |
|------|------|----------|
| private | 所有访问需鉴权 | 生产数据、敏感文件 |
| public-read | 公开可读，写入需鉴权 | 静态网站、CDN 资源 |
| public-read-write | 完全公开 | 不推荐用于生产 |

### RAM Policy 控制

- 应用服务使用 RAM 角色（而非 AK）访问 OSS
- 按最小权限原则：只授予必要的 `oss:PutObject`、`oss:GetObject` 等权限
- 禁止应用服务使用 `oss:DeleteBucket` 等危险权限

### 防盗链

通过 `RefererConfiguration` 设置 Referer 白名单，`AllowEmptyReferer: false` 禁止空 Referer 访问。

## 生命周期规则

通过 `LifecycleConfiguration.Rules` 配置：
- `Expiration.Days`：指定天数后自动删除
- `Transitions`：指定天数后转为低频（IA）或归档（Archive）存储类型

## 静态网站托管

通过 `WebsiteConfiguration` 配置 `IndexDocument` 和 `ErrorDocument`。

## CDN 加速

- OSS Bucket 绑定 CDN 域名，加速静态资源访问
- CDN 回源协议选 HTTPS，开启回源鉴权
- 缓存规则：静态资源（图片/JS/CSS）TTL 7 天，HTML 文件 TTL 10 分钟

## 跨区域复制（CRR）

- 关键数据开启跨区域复制，实现容灾
- 两端 Bucket 均需开启版本控制

## 最佳实践

- 生产 Bucket 禁止公网写入，通过 STS Token 临时授权
- 重要数据开启版本控制，防止误覆盖或误删
- 日志 Bucket 独立于业务 Bucket，避免权限混用
- 大文件（> 1 GB）使用分片上传（Multipart Upload）
- 定期审计 Bucket 权限，防止权限漂移
