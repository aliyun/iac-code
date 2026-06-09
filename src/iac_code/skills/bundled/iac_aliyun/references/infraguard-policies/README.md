# InfraGuard Policy Catalog

This directory contains generated InfraGuard Rego policies for the `iac-aliyun` skill.

- Total Rego files: 126
- Total rule policies: 117
- Scenarios: 8
- Scenario policy files: 117
- Packs: 8 (one scenario pack per scenario)

Scenario directories:

- `security` - security controls (30 rules)
- `high-availability` - high availability controls (13 rules)
- `cost-optimization` - cost optimization controls (13 rules)
- `compliance` - compliance controls (7 rules); pack reuses shared security baseline rules
- `best-practice` - best practice controls (13 rules)
- `operations` - operability controls (13 rules)
- `network-architecture` - network architecture controls (13 rules)
- `elasticity` - elasticity controls (15 rules)
- `packs` - one InfraGuard pack per scenario
- `lib` - shared Rego helpers

Regenerate with:

```bash
python3 src/iac_code/skills/bundled/iac_aliyun/scripts/generate_infraguard_policies.py
```

Validate a policy when InfraGuard is installed:

```bash
infraguard policy validate src/iac_code/skills/bundled/iac_aliyun/references/infraguard-policies/security/ecs-running-instance-no-public-ip.rego
```
