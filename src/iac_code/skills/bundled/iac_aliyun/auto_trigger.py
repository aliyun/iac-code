"""Auto-trigger rules for the bundled iac-aliyun skill."""

from __future__ import annotations

import re

ENABLE_AUTO_TRIGGER = True

_ALIYUN_SCOPE_PATTERNS = [
    r"阿里云",
    r"\baliyun\b",
    r"\balicloud\b",
    r"\balibaba\s+cloud\b",
    r"资源编排",
    r"\bresource\s+orchestration\s+service\b",
    r"rostemplateformatversion",
    r"aliyun::",
    r"datasource::",
    r"alicloud\s+provider",
    r'provider\s+"alicloud"',
    r'resource\s+"alicloud_',
    r"\becs\b",
    r"\brds\b",
    r"\boss\b",
    r"\bvpc\b",
    r"\bslb\b",
    r"\balb\b",
    r"\bnlb\b",
    r"安全组",
    r"负载均衡",
    r"云资源",
]

_ES_TEMPLATE_ACTIONS = r"genera|generar|crea|crear|despliega|desplegar|explica|explicar|valida|validar|mejora|mejorar"
_FR_TEMPLATE_ACTIONS = (
    r"cr[eé]e|cr[eé]er|g[eé]n[eé]re|g[eé]n[eé]rer|d[eé]ploie|d[eé]ployer|"
    r"explique|expliquer|valide|valider|am[eé]liore|am[eé]liorer"
)
_DE_TEMPLATE_ACTIONS = (
    r"erstelle|erstellen|generiere|generieren|bereitstelle|bereitstellen|"
    r"erkl[aä]re|erkl[aä]ren|validiere|validieren|verbessere|verbessern"
)
_PT_TEMPLATE_ACTIONS = r"gere|gerar|crie|criar|implante|implantar|explique|explicar|valide|validar|melhore|melhorar"
_ZH_IAC_NOUNS = r"模[板版]|资源栈|\bros\b|\bterraform\b"

_IAC_WORKFLOW_PATTERNS = [
    r"\bterraform\b",
    r"\bros[-\s]+template\b",
    r"\b(create|generate|write|deploy|explain|validate|improve|update|delete)\b.*\b(template|stack)\b",
    r"\b(template|stack)\b.*\b(create|generate|write|deploy|explain|validate|improve|update|delete)\b",
    r"ros\s*模[板版]",
    r"模板生成",
    r"模版生成",
    r"生成.*模[板版]",
    r"编写.*模[板版]",
    r"写.*模[板版]",
    r"解释.*模[板版]",
    r"完善.*模[板版]",
    r"校验.*模[板版]",
    r"验证.*模[板版]",
    r"更新.*模[板版]",
    r"删除.*模[板版]",
    r"资源栈",
    rf"部署.*({_ZH_IAC_NOUNS})",
    rf"({_ZH_IAC_NOUNS}).*部署",
    rf"({_ES_TEMPLATE_ACTIONS}).*plantilla",
    rf"plantilla.*({_ES_TEMPLATE_ACTIONS})",
    r"plantilla\s+ros",
    rf"({_FR_TEMPLATE_ACTIONS}).*mod[eè]le",
    rf"mod[eè]le.*({_FR_TEMPLATE_ACTIONS})",
    r"mod[eè]le\s+ros",
    rf"({_DE_TEMPLATE_ACTIONS}).*vorlage",
    rf"vorlage.*({_DE_TEMPLATE_ACTIONS})",
    r"ros[-\s]*vorlage",
    r"(生成|作成|デプロイ|説明|検証|改善|更新|削除).*テンプレート",
    r"テンプレート.*(生成|作成|デプロイ|説明|検証|改善|更新|削除)",
    r"ros\s*テンプレート",
    rf"({_PT_TEMPLATE_ACTIONS}).*modelo",
    rf"modelo.*({_PT_TEMPLATE_ACTIONS})",
    r"modelo\s+ros",
    r"\bcreatestack\b",
    r"\bvalidatetemplate\b",
    r"\.tf\b",
    r"\.ros\.ya?ml\b",
]

_PAC_WORKFLOW_PATTERNS = [
    r"\binfraguard\b",
    r"\brego\b",
    r"\bpolicy\s+as\s+code\b",
    r"\bpac\b",
    r"\bpack:aliyun:",
    r"\brule:aliyun:",
    r"\bcompliance\s+polic(y|ies)\b",
    r"\bpolicy\s+(list|get|update|validate)\b",
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
]


def should_trigger(prompt: str) -> bool:
    text = prompt.casefold()
    if has_pac_workflow(text):
        return False
    return has_aliyun_scope(text) and has_iac_workflow(text)


def has_aliyun_scope(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _ALIYUN_SCOPE_PATTERNS)


def has_iac_workflow(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _IAC_WORKFLOW_PATTERNS)


def has_pac_workflow(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _PAC_WORKFLOW_PATTERNS)
