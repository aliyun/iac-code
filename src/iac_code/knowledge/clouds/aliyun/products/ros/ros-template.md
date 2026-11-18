# ROS 模板最佳实践

## 常用资源

- ALIYUN::ECS::VPC: 创建专有网络
- ALIYUN::ECS::VSwitch: 创建交换机
- ALIYUN::ECS::SecurityGroup: 创建安全组（同时支持安全组规则）
- ALIYUN::ECS::InstanceGroup: 创建N个ECS实例（通过 `MaxAmount` 指定数量）

## 在实例中执行命令

**不要使用 UserData + WaitCondition**。根据场景选择：

- **自定义命令** → `ALIYUN::ECS::RunCommand` + `CommandContent`
- **公共命令** → `ALIYUN::ECS::Invocation` + `CommandName`

```yaml
# 自定义命令
RunSetup:
  Type: ALIYUN::ECS::RunCommand
  Properties:
    InstanceIds:
      - !Ref WebServer
    Type: RunShellScript
    Sync: true
    Timeout: 600
    ContentEncoding: PlainText
    CommandContent: |
      #!/bin/bash
      yum install -y nginx
      systemctl enable nginx
      systemctl start nginx

# 公共命令
InstallOpenClaw:
  Type: ALIYUN::ECS::Invocation
  Properties:
    InstanceIds:
      - !Ref WebServer
    CommandName: ACS-ECS-InstallOpenClaw-for-linux.sh
    Sync: true
    Timeout: 600
```

### 嵌套栈

```yaml
Resources:
  NetworkStack:
    Type: ALIYUN::ROS::Stack
    Properties:
      TemplateBody:
      Parameters:
        CidrBlock: 192.168.0.0/16
```

## 条件部署

通过 Conditions 实现环境差异化：

```yaml
Conditions:
  IsProd: !Equals [!Ref Env, prod]
Resources:
  ProdBucket:
    Type: ALIYUN::OSS::Bucket
    Condition: IsProd
    Properties:
      BucketName: prod-bucket
```

## 资源引用

- 通过 `!Ref` 和 `!GetAtt` 引用参数和资源属性，避免硬编码
- 使用 `DependsOn` 声明隐式依赖无法表达的顺序关系

## 常用函数

基础函数（Ref、Fn::GetAtt、Fn::Join、Fn::If、Fn::Equals 等）LLM 已熟悉，以下是容易用错的复杂函数。其他函数参考 aliyun_doc_search(keywords="ROS 函数", category_id=28850)。

### Fn::Sub

字符串变量替换。`${VarName}` 支持引用参数、伪参数（如 `${ALIYUN::StackId}`）、资源属性（如 `${Resource.Attr}`）。用 `${!VarName}` 保留字面量不替换。

```yaml
# 简写
!Sub "string-${VarName}"

# 带自定义变量映射
Fn::Sub:
  - "string-${Var1}-${Var2}"
  - Var1: value1
    Var2: value2
```

### Fn::Select

从列表按索引/切片，或从字典按 Key 选取。索引从 0 开始，支持负数。第三参数为默认值（可选）。

```yaml
# 按索引
!Select [index, list, defaultValue]

# 切片（start 默认 0，stop 默认 N，step 默认 1）
!Select ["start:stop:step", list]

# 按 Key
!Select [key, map, defaultValue]
```

### Fn::Replace

替换字符串中的子串，支持多个替换映射。

```yaml
Fn::Replace:
  - oldStr1: newStr1
    oldStr2: newStr2
  - originalString
```

### Fn::Jq

对 JSON 数据执行 jq 查询。

```yaml
Fn::Jq:
  - First/All
  - jqScript
  - jsonObject
```

- **First**：返回 jq 查询的第一个匹配值（字符串）
- **All**：返回 jq 查询的所有匹配值（列表）

jqScript 支持 jq 完整语法，包括管道 `|`、过滤 `select()`、变换 `{key: .field}` 等。

## Outputs

所有输出变量必须定义 Label。应用访问链接使用 `Console.` 前缀，会在 ROS 控制台概览页的「使用信息」中展示：

```yaml
Outputs:
  EcsPublicIp:
    Description: ECS 公网 IP
    Label: ECS Public IP
    Value: !GetAtt MyEcs.PublicIp
  Console.NginxUrl:
    Description: Nginx 访问地址
    Label: Nginx URL
    Value:
      !Sub
        - http://${Ip}
        - Ip: !Select [0, !GetAtt MyEcs.PublicIps]
```
