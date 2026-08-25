# StartChat permission-wait real E2E prompts

The runner substitutes `{run_id}`, `{stack_name}`, `{vswitch_name}`, and
`{mode}`. Each scenario uses a new Qoder session and an isolated workspace.

## Deployment

```text
请使用 alicloud-ros-agent Skill 的 {mode} 模式完成这个真实测试：在 cn-hangzhou 查询已有 VPC，选择其中一个，
只在该 VPC 内通过 ROS Stack 部署一个新 VSwitch。Stack 名称必须是 {stack_name}，VSwitch 名称必须是
{vswitch_name}，CIDR 不得与已有网段冲突。不要创建或删除 VPC，也不要修改其他资源。

执行过程中请持续用简短文字解释当前阶段。只读云查询不应申请权限；任何非只读操作都必须等待我明确确认。
部署确认前必须展示部署摘要和 Mermaid 架构图。Pipeline 模式必须生成恰好两个都满足约束且确有差异的候选
方案：优先使用不同可用区；若只能使用同一可用区，则使用两个不同且均不冲突的 VSwitch CIDR。不得把同一
方案仅重命名凑数，也不得在候选选择前合并为一个；最终只部署用户选择的一个方案。Pipeline 模式还必须展示
step 开始/结束和候选选择，并在完成后保留同一 job 的 Normal handoff。部署完成后先报告 Stack、VSwitch 和
所选已有 VPC，不要自动清理。
```

## Continue deployment

```text
请继续同一个 ROS Agent job 完成原任务。若正在等待选择 VPC，请选择返回列表中第一个没有 VSwitch、且能容纳
不冲突网段的已有 VPC；只需保留该 VPC 的精简摘要，不要再次返回完整 VPC 列表。继续生成并校验只含一个
VSwitch 的 ROS Stack {stack_name}，VSwitch 名称为 {vswitch_name}。在任何部署确认之前，先用简短说明和
Mermaid 架构图展示已有 VPC 与待建 VSwitch 的关系；确认和非只读云操作都必须等待我的明确回答。
```

## Confirm deployment

```text
我已经审阅刚才展示的部署摘要和 Mermaid 架构图，确认仅在所选已有 VPC 中通过 Stack {stack_name} 创建
VSwitch {vswitch_name}。请继续同一个 ROS Agent job；遇到非只读云权限时仍需把权限申请返回给我，不得替我批准。
```

## Cleanup

```text
请继续同一个 ROS Agent job，清理本次测试创建的 Stack {stack_name} 及其中的 VSwitch {vswitch_name}。
删除前说明目标并等待我确认；只允许删除这两个本次创建的对象，绝不能删除或修改已有 VPC。Pipeline 场景必须
复用 Pipeline handoff 的 Normal 会话，不得启动新的 Normal job。清理后用只读查询确认 Stack/VSwitch 已不存在，
并确认原有 VPC 仍可用。先展示精简删除摘要并等待我下一条明确确认。
```

## Confirm cleanup

```text
我确认删除本次测试创建的 Stack {stack_name}，并让其中的 VSwitch {vswitch_name} 随 Stack 删除。请继续同一个
ROS Agent job；不得删除或修改已有 VPC，遇到非只读云权限时仍需把权限申请返回给我，不得替我批准。
```

## Scripted answers

The headless runner resumes the same Qoder session with exactly one of these
bounded answers, selected from the current bridge `inputRequired.kind`:

- permission: `允许当前明确展示且属于本次范围的非只读操作，仅允许一次，然后继续同一个 job。`
- ask_user_question: `选择当前问题中的第一个已有 VPC；如果是部署确认，则确认部署。继续同一个 job。`
- candidate_selection: `选择推荐候选；若没有明确推荐，选择第一个候选。继续同一个 job。`
- active Pipeline follow: `只对当前 job 调用 follow 继续观察，不要发送自然语言 continue 来催促远端。`

If a permission is read-only, the runner fails instead of answering it. If a
permission target is outside the exact run-scoped Stack/VSwitch, the operator
must deny it and stop the run.
