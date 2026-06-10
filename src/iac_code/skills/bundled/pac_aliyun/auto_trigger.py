"""Auto-trigger rules for the bundled pac-aliyun skill."""

from __future__ import annotations

import re

ENABLE_AUTO_TRIGGER = True

_ALIYUN_SCOPE_PATTERNS = [
    r"阿里云",
    r"\baliyun\b",
    r"\balicloud\b",
    r"\balibaba\s+cloud\b",
    r"\bros\b",
    r"rostemplateformatversion",
    r"aliyun::",
    r"\becs\b",
    r"\brds\b",
    r"\boss\b",
    r"\bvpc\b",
    r"\bslb\b",
    r"\balb\b",
    r"\bnlb\b",
    r"安全组",
    r"云资源",
]

_PAC_SCOPE_PATTERNS = [
    r"\binfraguard\b",
    r"\brego\b",
    r"\bpolicy\s+as\s+code\b",
    r"\bpac\b",
    r"\bpack:aliyun:",
    r"\brule:aliyun:",
    r"合规策略",
    r"策略库",
    r"策略包",
]

_POLICY_WORKFLOW_PATTERNS = [
    r"\binfraguard\b",
    r"\brego\b",
    r"\bpolicy\s+as\s+code\b",
    r"\bpac\b",
    r"\bpack:aliyun:",
    r"\brule:aliyun:",
    r"\binfraguard\s+(scan|policy)\b",
    r"\bpolicy\s+(list|get|update|validate)\b",
    r"\b(scan|validate|check)\b.*\bcompliance\s+polic(y|ies)\b",
    r"\b(generat|writ|creat)e?\b.*\bcompliance\s+polic(y|ies)\b",
    r"\bcompliance\s+polic(y|ies)\b.*\b(generat|writ|creat|validat|check)e?\b",
    r"合规策略",
    r"策略生成",
    r"生成.*策略",
    r"编写.*策略",
    r"写.*策略",
    r"校验.*策略",
    r"验证.*策略",
    r"检查.*策略",
    r"策略.*校验",
    r"策略.*验证",
    r"策略.*检查",
    r"高可用.*策略",
    r"成本优化.*策略",
    r"合规性.*策略",
    r"最佳实践.*策略",
    r"可运维.*策略",
    r"网络架构.*策略",
    r"弹性.*策略",
]


def should_trigger(prompt: str) -> bool:
    text = prompt.casefold()
    return has_policy_workflow(text) and (has_pac_scope(text) or has_aliyun_scope(text))


def has_aliyun_scope(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _ALIYUN_SCOPE_PATTERNS)


def has_pac_scope(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _PAC_SCOPE_PATTERNS)


def has_policy_workflow(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _POLICY_WORKFLOW_PATTERNS)
