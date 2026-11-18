# 阿里云 SGX/TDX 支持范围

[通用原理](knowledge://common/hardware/confidential-computing.md)


| 能力 | 保护范围与规格识别 |
|------|------------------|
| SGX | 保护应用 enclave；`g7t/c7t/r7t`、`g9it/r9it` 是 SGX 规格族，不能把 `r9it` 当成 `r9i` 的 TDX 替代品 |
| TDX | 保护整台虚拟机；须同时匹配官方支持的地域、可用区、实例规格与镜像，并设置 `SecurityOptions.ConfidentialComputingMode=TDX` |

[TDX 官方支持表](https://help.aliyun.com/zh/ecs/user-guide/build-a-tdx-confidential-computing-environment)的部分对照（不是实时库存）：

| 地域与可用区 | TDX 支持规格 |
|--------------|--------------|
| 杭州 J | `ecs.r9i.xlarge` 及以上 |
| 北京 L | `ecs.g8i.xlarge` 及以上 |
| 北京 I | `ecs.g8i.xlarge`、`ecs.g9i.xlarge`、`ecs.c9i.xlarge`、`ecs.r9i.xlarge` 及以上，以及 `ebmg8i` 规格族 |
| 中国香港 B/C/D、新加坡 B | `ecs.g8i.xlarge` 及以上 |

`r9i` 是 Granite Rapids／Intel Xeon 6，不是 Sapphire Rapids 或 Emerald Rapids；参见 [CPU 与实例规格](knowledge://clouds/aliyun/products/ecs/hardware.md)。

有库存、支持 UEFI、安全启动或 vTPM 都不能单独证明支持 TDX，也不能把上述支持范围推广到整个规格族或所有地域。用户质疑已核实的规格时，对照官方支持表解释；有新依据再调整。

`DescribeAvailableResource` 没有 TDX 筛选参数，普通库存查询结果不能证明 TDX 可售性。

SGX 依据：[增强型实例规格](https://help.aliyun.com/zh/ecs/user-guide/enhanced-instance-families/)、[SGX 环境指南](https://help.aliyun.com/zh/ecs/user-guide/build-an-sgx-encrypted-computing-environment/)。
