# ECS CPU 与实例规格

| 对象 | 已核实事实 |
|------|------------|
| `g8i/c8i/r8i` | 第四代或第五代 Intel Xeon 可扩展处理器（Sapphire Rapids / Emerald Rapids），具体型号按 API 核实 |
| `g9i/c9i/r9i` | Intel Xeon 6；ECS 的“9 代”不是“第九代至强”，也不是酷睿 |
| `r9i` | Granite Rapids，不是 Sapphire Rapids 或 Emerald Rapids |
| `ecs.r9i.xlarge` | 4 vCPU、32 GiB；8 vCPU、64 GiB 对应 `ecs.r9i.2xlarge` |

依据：[Intel 实例代际](https://help.aliyun.com/zh/ecs/intel-instance-specifications-and-operating-system-compatibility)、[r9i 规格表](https://help.aliyun.com/zh/ecs/user-guide/memory-optimized-instance-families-1)。

查询 [DescribeInstanceTypes](https://help.aliyun.com/zh/ecs/developer-reference/api-ecs-2014-05-26-describeinstancetypes) 时，用 `InstanceTypeId` 对齐记录；`CpuCoreCount` 是 vCPU 数，`MemorySize` 单位为 GiB，`PhysicalProcessorModel` 是处理器型号。型号尚未对应到官方代际时，原样引用型号，不自行补充代际。
