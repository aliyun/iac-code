# 增强型公网 NAT 网关 ROS 参考

## 快速导航

| 需求 | 优先查看 | 重点核对 |
| --- | --- | --- |
| 创建增强型公网 NAT 网关 | `ALIYUN::VPC::NatGateway` | `NatType`、`NetworkType`、计费取值与 `VpcId` / `VSwitchId` 引用 |
| 绑定已有 EIP | `ALIYUN::VPC::EIPAssociation` | `AllocationId`、`InstanceId`、`Mode` 的资源归属与绑定后的 `EipAddress` |
| SNAT 出网 | `ALIYUN::VPC::SnatEntry` | `SNatTableId`、`SourceCIDR` / `SourceVSwitchIds` 互斥与 `EipAddress` 引用 |
| DNAT 端口映射 | `ALIYUN::VPC::ForwardEntry` | `ForwardTableId`、字符串端口、协议与 `Any` 端口配对 |
| 资源依赖与引用 | “资源关系与函数引用” | `Ref` / `Fn::GetAtt` 的合法目标、表 ID 与绑定后 EIP 地址 |
| 已有模板局部修改 | “已有模板的局部修改” | 官方可更新标量、同形标量与授权路径 |
| 输出 | “输出写法” | 已声明资源、合法返回属性、大小写与单复数 |
| 字段冲突或端口规则不确定 | “常见错误与修复” | 精确字段名、所属资源、互斥条件与官方约束 |

### 最小查询与修改流程

1. 先区分新建/补齐模板与已有模板局部修改。局部修改必须先取得完整旧模板、授权路径和必须保留的命名与结构，不把两类任务混用。
2. 查询顺序为：用户题干与指定命名 → 本页导航和字段表 → 目标官方 ROS schema。仅当本页未覆盖、存在版本差异、取值冲突或具体失败需要定位时，才用完整资源类型名加字段名查询官方文档；不要从 API / SDK 参数名推断 `Properties` 层级。
3. 使用结构化 JSON 解析和完整对象路径定位，避免重复读取无关文件。写入后重新读取实际文件并解析；用户要求逐字保留或顺序不变时，还要对比实际序列化文本，而不是只比较语义 JSON。
4. 失败时先读取实际错误、当前 JSON 和目标路径，区分 JSON 语法错误、字段名 / 层级错误、引用错误、取值冲突和官方 schema 未确认。只修复错误指出的最小必要内容；官方文档查询失败时最多用更精确的资源类型加字段名重试一次，仍失败则停止并说明未确认字段。

## 适用范围与使用原则

本页覆盖 `ALIYUN::VPC::NatGateway`、`ALIYUN::VPC::EIPAssociation`、`ALIYUN::VPC::SnatEntry` 与 `ALIYUN::VPC::ForwardEntry` 组成的增强型公网 NAT 网关模板。模板不创建 VPC、交换机或 EIP 本身；这些环境输入通过既有参数传递。该范围只约束本页介绍的目标资源，不得据此删除或改名用户模板中的非目标资源。

- 完整 JSON 模板的 `ROSTemplateFormatVersion` 固定为 `"2015-09-01"`。除非用户明确要求，不添加 `Transform`、`Mappings`、`Rules`、`Conditions` 等顶层结构。
- 已有 VPC、NAT 网关交换机、已有 EIP 的 `AllocationId`、SNAT 源交换机、DNAT 私网目标 IP 等环境输入使用必填参数且不设置 `Default`。本页目标场景的 ID / IP 输入使用 `String`；用户指定 `Type` 时逐字采用。不要写真实云资源 ID、固定业务值或凭据值。
- 生成或修改模板时，只写入用户要求、官方必填或条件必填的属性；可选能力仅在用户需求或官方条件满足时添加，不要为了“完整 schema”主动补齐默认值。
- 用户明确指定的资源逻辑 ID、参数名和输出名必须逐字保留。`Ref` / `Fn::GetAtt` 必须使用这些 ID；不得自行改名、添加后缀或用示例名称替代。
- 本页不是完整 schema。未覆盖字段、版本差异或取值冲突，应查询目标官方 ROS schema；无法确认时明确说明，不要猜测或放宽校验。

## 资源关系与函数引用

### 组合选择与数据流

- 只创建网关时，不自动补齐未请求的 EIP 绑定、SNAT、DNAT、输出或可选资源。
- 需要用已有 EIP 提供公网地址时，使用 `ALIYUN::VPC::EIPAssociation`，不创建 EIP 本身；SNAT 出网再添加 `ALIYUN::VPC::SnatEntry`，DNAT 端口映射再添加 `ALIYUN::VPC::ForwardEntry`。
- 用户要求同一模板同时出网和入网时，复用同一个网关和同一个 EIP 绑定，分别创建 SNAT 与 DNAT 条目；除非用户明确要求，不新增额外网关或 EIP 绑定。
- 典型数据流：网关使用环境参数；已有 EIP 绑定到本模板网关；SNAT / DNAT 读取网关返回表 ID 与绑定后的 EIP 地址；输出读取真实资源返回属性。优先用引用表达隐式依赖，仅在引用无法表达且确有顺序要求时才考虑 `DependsOn`。

| 用途 | 正确写法 |
| --- | --- |
| 网关 `VpcId` / `VSwitchId` | `{"Ref": "<环境参数名>"}` |
| EIP 绑定 `AllocationId` | `{"Ref": "<已有EipAllocationId参数名>"}` |
| EIP 绑定 `InstanceId` | `{"Ref": "<NatGateway逻辑ID>"}` |
| SNAT 表 | `{"Fn::GetAtt": ["<NatGateway逻辑ID>", "SNatTableId"]}` |
| DNAT 表 | `{"Fn::GetAtt": ["<NatGateway逻辑ID>", "ForwardTableId"]}` |
| SNAT / DNAT 公网 IP | `{"Fn::GetAtt": ["<EIPAssociation逻辑ID>", "EipAddress"]}` |
| DNAT 私网目标 IP | `{"Ref": "<私网目标IP参数名>"}` |

- `Ref` 可以引用已声明资源、已声明参数或 ROS 支持的伪参数。
- `Fn::GetAtt` 只能获取已声明资源的合法返回属性；不能把参数或伪参数作为被取属性的资源。两者的合法目标不能合并。
- SNAT / DNAT 需要公网 IP 时，读取本模板 `EIPAssociation` 的 `EipAddress`，使规则依赖绑定结果；不要把 `AllocationId` 当作 IP 地址。
- List 属性把完整数组写在父属性上，例如 `SourceVSwitchIds: [{"Ref": "<参数名>"}]`；断言和修改都不使用数组下标路径。

## ALIYUN::VPC::NatGateway（NAT 网关）

| 属性路径 | 类型 / 取值 | 说明与更新约束 |
| --- | --- | --- |
| `Properties.VpcId` | String | 必填；网关所属 VPC，不可更新。 |
| `Properties.VSwitchId` | String | 必填；增强型网关在该交换机内分配私网 IP，不可更新。 |
| `Properties.NatType` | `"Enhanced"` | 本页增强型网关必须显式设置。 |
| `Properties.NetworkType` | `"internet"` | 本页公网 NAT 网关必须显式设置。 |
| `Properties.InstanceChargeType` | `"PostPaid"` | 本页按量付费目标必须显式设置。 |
| `Properties.InternetChargeType` | `"PayByLcu"` | 本页按 LCU 计费目标必须显式设置。 |
| `Properties.NatGatewayName` | String | 可选；长度 2~128，以字母或汉字开头。 |
| `Properties.Description` | String | 可选；长度 2~256。 |
| `Properties.DeletionProtection` | Boolean | 可选；官方标记可更新。 |
| `Properties.Tags` | List | 可选；官方标记可更新，不是局部标量修改目标。 |

官方标记允许更新的属性只有 `DeletionProtection` 与 `Tags`，其余属性均不可更新。官方返回属性为 `NatGatewayId`、`SNatTableId`、`ForwardTableId`；注意 `SNatTableId` 的大小写。`Mode` 只属于 `EIPAssociation`，`NatGateway` 没有 `EipBindMode` 或等价绑定模式属性。

## ALIYUN::VPC::EIPAssociation（EIP 绑定）

| 属性路径 | 类型 / 取值 | 说明与更新约束 |
| --- | --- | --- |
| `Properties.AllocationId` | String | 必填；已有 EIP 实例 ID，引用输入参数。官方标记可更新。 |
| `Properties.InstanceId` | String | 必填；绑定本模板网关时引用网关逻辑 ID。官方标记可更新。 |
| `Properties.PrivateIpAddress` | String | 可选；仅在用户要求指定私网地址时添加。官方标记可更新。 |
| `Properties.Mode` | `"NAT"` / `"MULTI_BINDED"` / `"BINDED"` | 可选；EIP 绑定模式，属于本资源。官方标记可更新。 |

官方标记允许更新的属性为上表四项。官方返回属性为 `EipAddress`、`AllocationId`。本资源只绑定已有 EIP，不创建 EIP。

## ALIYUN::VPC::SnatEntry（SNAT 条目）

| 属性路径 | 类型 / 取值 | 说明与更新约束 |
| --- | --- | --- |
| `Properties.SnatTableId` | String | 必填；读取网关 `SNatTableId`，不可更新。 |
| `Properties.SnatIp` | String | 必填；公网 IP。多个 IP 用半角逗号分隔且不含空格；它是字符串标量，不是数组。官方标记可更新。 |
| `Properties.SourceCIDR` | String | 条件选择；与 `SourceVSwitchIds` 互斥，不可更新。 |
| `Properties.SourceVSwitchIds` | List | 条件选择；与 `SourceCIDR` 互斥，完整数组写在父属性上。官方标记可更新。 |
| `Properties.SnatEntryName` | String | 可选；长度 2~128，以字母或汉字开头。官方标记可更新。 |
| `Properties.EipAffinity` | 以官方 schema 为准 | 可选；语义或取值未确认时先查询官方文档。 |
| `Properties.NetworkInterfaceId` | String | 可选；仅在用户明确要求该源方式时添加，修改前确认官方更新约束。 |

官方中文文档返回属性是复数形式 `SnatEntryIds`，不要写成 `SnatEntryId`。`SourceCIDR` 与 `SourceVSwitchIds` 不能同时出现，也不要同时补齐。

## ALIYUN::VPC::ForwardEntry（DNAT 条目）

| 属性路径 | 类型 / 取值 | 说明与更新约束 |
| --- | --- | --- |
| `Properties.ForwardTableId` | String | 必填；读取网关 `ForwardTableId`。 |
| `Properties.ExternalIp` | String | 必填；公网 IP，通常读取 `EIPAssociation.EipAddress`。 |
| `Properties.ExternalPort` | String | 必填；单端口 1~65535，端口段用 `/` 分隔起止端口。 |
| `Properties.InternalIp` | String | 必填；VPC 内目标私网 IP，通常引用参数。 |
| `Properties.InternalPort` | String | 必填；单端口 1~65535，端口段用 `/` 分隔起止端口。 |
| `Properties.IpProtocol` | `"TCP"` / `"UDP"` / `"Any"` | 必填协议。 |
| `Properties.PortBreak` | Boolean | 可选；仅在 DNAT 与 SNAT 复用同一公网 IP 且需要配置大于 1024 的端口时设为 `true`。 |

端口规则：`ExternalPort` 与 `InternalPort` 都是字符串；端口段的外部与内部端口个数必须相同；`IpProtocol` 为 `Any` 时，两侧端口都必须写 `"Any"`，对应 DNAT IP 全映射，不能与单端口或端口段混用。

官方返回属性是 `ForwardEntryId`。当前官方文档未将 `ForwardEntry` 属性标记为允许更新，局部修改不要选择 DNAT 字段作为目标。若目标官方 schema 已更新，以最新官方说明为准。

## 常见错误与修复

| 症状 / 易错点 | 正确处理 |
| --- | --- |
| SNAT 表或条目输出大小写 / 单复数错误 | 网关返回属性使用 `SNatTableId`；SNAT 条目返回属性使用复数 `SnatEntryIds`。不要写成 `SnatTableId` 或 `SnatEntryId`。 |
| 同时写了 `SourceCIDR` 和 `SourceVSwitchIds` | 二者互斥。用户要求交换源方式时，先确认目标 schema 与授权范围；局部修改不要同时补齐或删除另一个属性。 |
| 把 `Mode` 写到网关或把网关属性当作绑定模式 | `Mode` 只属于 `ALIYUN::VPC::EIPAssociation`；`ALIYUN::VPC::NatGateway` 没有 `EipBindMode` 或等价属性。 |
| 把网关 `VSwitchId` 当作 SNAT 源范围 | `NatGateway.Properties.VSwitchId` 只用于增强型网关在交换机内分配私网 IP；SNAT 源范围写在 `SnatEntry.Properties.SourceVSwitchIds` 或 `SourceCIDR`，且二者互斥。 |
| 端口写成数字，或 `Any` 只写在一侧 | `ExternalPort` 与 `InternalPort` 都是字符串；端口段两侧个数相同；`IpProtocol: "Any"` 时两侧端口都必须是 `"Any"`。 |
| 把 `AllocationId` 当公网 IP，或把网关 ID 当 EIP ID | EIP 绑定的 `AllocationId` 引用已有 EIP 输入；SNAT / DNAT 的公网 IP 读取绑定资源返回的 `EipAddress`。 |
| 局部修改改到数组、对象或 `ForwardEntry` 字段 | 先核对官方可更新属性和用户授权路径。当前官方文档未标记 `ForwardEntry` 属性可更新；数组 / 对象整体不是标量修改目标。 |
| 引用目标混用 | `Ref` 可指向已声明资源、参数或支持的伪参数；`Fn::GetAtt` 只能指向已声明资源及其合法返回属性。 |

## 输出写法

`Outputs.Value` 精确引用当前模板已声明资源的 `Ref` 或官方 `Fn::GetAtt` 返回属性，不要硬编码示例值，也不要改变属性名大小写或单复数。

| 输出语义 | 正确来源 |
| --- | --- |
| 网关 ID | `{"Fn::GetAtt": ["<NatGateway逻辑ID>", "NatGatewayId"]}` |
| SNAT 表 ID | `{"Fn::GetAtt": ["<NatGateway逻辑ID>", "SNatTableId"]}` |
| DNAT 转发表 ID | `{"Fn::GetAtt": ["<NatGateway逻辑ID>", "ForwardTableId"]}` |
| 绑定后的 EIP | `{"Fn::GetAtt": ["<EIPAssociation逻辑ID>", "EipAddress"]}` |
| SNAT 条目 | `{"Fn::GetAtt": ["<SnatEntry逻辑ID>", "SnatEntryIds"]}` |
| DNAT 条目 | `{"Fn::GetAtt": ["<ForwardEntry逻辑ID>", "ForwardEntryId"]}` |

如果用户要求输出使用 `Ref`，仅在 `Ref` 的返回语义符合该输出要求时使用；`Ref` 与 `Fn::GetAtt` 不是可互换的取属性方式。

## 已有模板的局部修改

用户提供完整已有模板并要求局部修改时：

1. 只修改用户明确授权且旧模板中已经存在的资源 `Properties.<PropertyName>` 标量叶子；路径必须指向字符串、数字或布尔值，且属性必须被官方标记为允许更新。
2. 新值仍是同形标量。不得把标量改成 `Ref` / `Fn::GetAtt` 对象，也不得把既有引用对象改回标量。
3. 不得把数组或对象整体列为允许变化路径，不得用下标定位数组元素；旧模板中的数组元素、顺序、对象和既有引用必须原样保留。
4. 不新增或删除资源、参数、输出、属性、顶层字段；不重排数组；不给参数补 `Default`；保留用户命名与未授权结构。
5. 官方可更新标量包括 `NatGateway.DeletionProtection`，`EIPAssociation.AllocationId`、`InstanceId`、`PrivateIpAddress`、`Mode`，以及 `SnatEntry.SnatIp`、`SnatEntryName`。`Tags` 与 `SourceVSwitchIds` 是 List；`ForwardEntry` 当前没有官方标记可更新属性。
6. 若校验说明提供 `preserve_template`、`allowed_changes` 或新值断言，把它们与完整对象路径一一对应：基线必须是用户给出的完整旧模板；`allowed_changes` 只能列出授权且官方允许更新的既有标量叶；新值断言只检查这些路径的同形标量结果。不要用通配路径、资源类型名或“语义等价”解释未授权差异。
7. 若目标必须改变对象、数组结构、资源组合或引用关系，应按新建模板需求处理，不要伪装成局部标量修改。路径不符合约束时保留原值并说明冲突。

## 必要离线校验

生成或修改后至少检查：

- `template.json` 可被 JSON 解析，`ROSTemplateFormatVersion` 为 `"2015-09-01"`；顶层键、资源类型、逻辑 ID、参数名与输出名逐字符合用户要求。
- 必填环境参数没有 `Default`，类型精确符合用户指定类型；未指定时本页目标场景的 ID / IP 输入为 `String`。目标资源数量和组合未被未授权改动。
- `NatGateway` 显式包含 `NatType: "Enhanced"`、`NetworkType: "internet"`、`InstanceChargeType: "PostPaid"`、`InternetChargeType: "PayByLcu"`。
- `EIPAssociation.InstanceId` 引用本模板网关，`AllocationId` 引用输入参数；SNAT / DNAT 公网 IP 读取绑定后的 `EipAddress`。
- `SnatEntry.SnatTableId` 使用 `SNatTableId`；`SourceCIDR` 与 `SourceVSwitchIds` 最多出现一个；`SnatIp` 保持字符串或既有引用形态。
- `ForwardEntry.ForwardTableId` 使用 `ForwardTableId`；端口为字符串，端口段个数匹配，`Any` 协议两侧端口均为 `"Any"`。
- 输出 `Value` 使用对应已声明资源的合法 `Ref` 或 `Fn::GetAtt`；数组断言作用于父属性整体，不用下标路径。
- 局部修改执行新旧模板对比，确认仅授权标量路径变化；用户要求逐字一致或顺序不变时，检查实际序列化文本而不只是语义 JSON。
- 写入成功后重新读取实际文件并解析，不要把最终回复描述当作文件已正确的证据。

这些检查只覆盖模板结构与取值。EIP 归属与状态、配额、交换机可用区是否支持增强型 NAT 网关、VPC 路由、私网目标 IP 归属以及真实出网 / 入站连通性均为未验证前置条件；不要宣称完整 schema 校验、库存、权限、部署或云上运行时健康已经通过。

## 官方来源

- [ALIYUN::VPC::NatGateway](https://help.aliyun.com/zh/ros/developer-reference/aliyun-vpc-natgateway)
- [ALIYUN::VPC::EIPAssociation](https://help.aliyun.com/zh/ros/developer-reference/aliyun-vpc-eipassociation)
- [ALIYUN::VPC::SnatEntry](https://help.aliyun.com/zh/ros/developer-reference/aliyun-vpc-snatentry)
- [ALIYUN::VPC::ForwardEntry](https://help.aliyun.com/zh/ros/developer-reference/aliyun-vpc-forwardentry)
- [ROS 模板结构](https://help.aliyun.com/zh/ros/developer-reference/template-structure)
