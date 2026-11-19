# 阿里云 GPU 识别与可售范围

[B70 硬件事实](knowledge://common/hardware/intel-arc-pro-b70.md)。Intel 已有该型号不等于阿里云已有对应实例；本资料不预设在售或未售结论。

云上 GPU 识别看 `DescribeInstanceTypes` 的 `GPUSpec`、`GPUAmount`，不能把 CPU 型号当成 GPU。规格目录与可购买库存分别核实；可售性再用目标地域、可用区和计费方式查询 `DescribeAvailableResource`。

未查全分页、只查一个地域或仅检索到部分文档时，结论限定为“在本次已查范围内未查到，其他范围尚未确认”；不能据此断言阿里云全球不提供 Intel GPU。库存为空也不能推出硬件型号不存在或产品不支持。
