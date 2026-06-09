# InfraGuard Policy Catalog

This directory contains generated InfraGuard Rego policies for the `iac-aliyun` skill.

- Total Rego files: 141
- Total rule policies: 131
- Scenarios: 8
- Scenario policy files: 106
- Cloud infrastructure security baseline rules: 25
- Packs: 9 (8 scenario packs + 1 cloud infrastructure security baseline pack)

Scenario directories:

- `security` - security controls (13 rules)
- `high-availability` - high availability controls (13 rules)
- `cost-optimization` - cost optimization controls (13 rules)
- `compliance` - compliance controls (13 rules)
- `best-practice` - best practice controls (13 rules)
- `operations` - operability controls (13 rules)
- `network-architecture` - network architecture controls (13 rules)
- `elasticity` - elasticity controls (15 rules)
- `packs` - one InfraGuard pack per scenario plus the cloud infrastructure security baseline pack
- `rules/ros` - cloud infrastructure security baseline rules
- `lib` - shared Rego helpers

Regenerate with:

```bash
python3 src/iac_code/skills/bundled/iac_aliyun/scripts/generate_infraguard_policies.py
```

Validate a policy when InfraGuard is installed:

```bash
infraguard policy validate src/iac_code/skills/bundled/iac_aliyun/references/infraguard-policies/security/security-ecs-instance-no-public-ip.rego
```
