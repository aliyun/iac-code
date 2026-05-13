---
title: Interactive Mode
description: Use the REPL for iterative infrastructure work.
---

# Interactive Mode

Run without arguments to enter the interactive REPL:

```bash
iac-code
```

Interactive mode is useful when you want to refine infrastructure requirements over multiple turns.

Start with authentication:

```text
/auth
```

Then describe what you want to build:

```text
Create a VPC, two ECS instances, and a security group that allows SSH from my office IP.
```
