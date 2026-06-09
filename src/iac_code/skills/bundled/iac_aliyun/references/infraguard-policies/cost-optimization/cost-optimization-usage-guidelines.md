# Cost Optimization InfraGuard Usage Guidelines

## Sources read

- Local official InfraGuard repository: `/private/tmp/infraguard-official`.
- Alibaba Cloud ROS and Well-Architected official references for the scenario.
- Cross-cloud Well-Architected references were used as control-point background when available.

## Write to Learn notes

The scenario was reduced to static IaC checks only. Runtime metrics, billing history, incident evidence, and approval workflow evidence are kept out of Rego because they cannot be proven from a ROS template alone.

## Engineer-facing guidelines

- Prefer explicit resource intent over provider defaults.
- Make the policy failure point actionable through `violation_path`.
- Keep rule ids short and stable, without scenario prefixes.
- Keep exceptions outside Rego unless the exception is represented in the template itself.

## Rego mapping

The scenario pack in `../packs/iac-code-cost-optimization-pack.rego` lists the rule ids enforced for this scenario. Rules use `package infraguard.rules.aliyun.<snake_case>`, `rule_meta`, and `deny contains result if` in the official InfraGuard style.
