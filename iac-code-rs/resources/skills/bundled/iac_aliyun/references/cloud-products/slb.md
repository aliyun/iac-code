# 阿里云负载均衡选型指南

## 产品对比

| 产品 | 层级 | 协议 | 适用场景 |
|------|------|------|----------|
| CLB（传统型） | L4/L7 | TCP/UDP/HTTP/HTTPS | 通用场景，成本低 |
| ALB（应用型） | L7 | HTTP/HTTPS/QUIC | 高级路由、微服务网关 |
| NLB（网络型） | L4 | TCP/UDP/TCPSSL | 超高性能、极低延迟 |

### 选型决策树

```
需要 L7 路由（URL/Header/Cookie 转发）？
├── 是 → ALB（应用型负载均衡）
│         - 基于内容的路由
│         - 重写/重定向
│         - WAF 集成
│         - WebSocket/HTTP2
└── 否 → 需要超高性能（百万 QPS / 极低延迟）？
          ├── 是 → NLB（网络型负载均衡）
          │         - 保持客户端源 IP
          │         - UDP 支持
          └── 否 → CLB（传统型负载均衡）
                    - 成本最低
                    - 配置简单
                    - TCP/HTTP 均支持
```

## CLB 配置要点

涉及 3 个资源类型：
- `ALIYUN::SLB::LoadBalancer`：创建实例，关键属性 `AddressType`（internet/intranet）、`LoadBalancerSpec`
- `ALIYUN::SLB::Listener`：配置监听，关键属性 `Protocol`、`ListenerPort`、`BackendServerPort`、`HealthCheck`
- `ALIYUN::SLB::BackendServerAttachment`：绑定后端服务器

## ALB 配置要点

涉及 3 个资源类型：
- `ALIYUN::ALB::LoadBalancer`：创建实例，需配置 `ZoneMappings`（至少 2 个可用区）
- `ALIYUN::ALB::ServerGroup`：配置服务器组和健康检查
- `ALIYUN::ALB::Listener`：配置监听，通过 `DefaultActions` 的 `ForwardGroup` 关联 ServerGroup

## HTTPS 配置建议

- 证书上传到阿里云证书管理服务（SSL Certificates Service）
- SLB/ALB 监听器配置 HTTPS，引用证书 ID
- HTTP → HTTPS 重定向：ALB 支持配置监听器重定向规则

## 健康检查最佳实践

- 配置专用健康检查接口（如 `/health`），返回 200 即视为健康
- 阈值设置：健康阈值 3 次，不健康阈值 3 次，间隔 5 秒
- 避免用业务接口做健康检查（防止误判）

## 会话保持

- CLB：支持基于 Cookie 的会话保持
- ALB：支持基于 Cookie 和源 IP 的会话保持
- 无状态应用：不需要会话保持，水平扩展更灵活
