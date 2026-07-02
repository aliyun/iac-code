use iac_code_protocol::message::{AgentContentBlock, AgentMessage, AgentMessageContent};

use crate::ToolContext;

use super::prompt::render_skill_prompt;
use super::{SkillManager, SkillSource};

pub(super) fn auto_triggered_messages(
    manager: &SkillManager,
    prompt: &str,
    context: &ToolContext,
    existing_messages: &[AgentMessage],
) -> Vec<AgentMessage> {
    let skill_name = "iac-aliyun";
    if !should_auto_trigger_iac_aliyun(prompt)
        || context_has_skill_tag(existing_messages, skill_name)
    {
        return Vec::new();
    }

    let Some(skill) = manager.get(skill_name) else {
        return Vec::new();
    };
    if skill.source != SkillSource::Bundled {
        return Vec::new();
    }

    vec![AgentMessage {
        role: "user".into(),
        content: AgentMessageContent::Text(format!(
            "<skill-name>{}</skill-name>\n\n{}",
            skill.name,
            render_skill_prompt(skill, "", context)
        )),
        token_count: 0,
        elapsed_seconds: 0.0,
    }]
}

pub(super) fn should_auto_trigger_iac_aliyun(prompt: &str) -> bool {
    let text = prompt.to_lowercase();
    !text.trim().is_empty() && has_aliyun_scope(&text) && has_iac_workflow(&text)
}

fn has_aliyun_scope(text: &str) -> bool {
    text.contains("阿里云")
        || contains_word(text, "aliyun")
        || contains_word(text, "alicloud")
        || contains_phrase_words(text, &["alibaba", "cloud"])
        || text.contains("资源编排")
        || contains_phrase_words(text, &["resource", "orchestration", "service"])
        || text.contains("rostemplateformatversion")
        || text.contains("aliyun::")
        || text.contains("datasource::")
        || contains_phrase_words(text, &["alicloud", "provider"])
        || text.contains("provider \"alicloud\"")
        || text.contains("resource \"alicloud_")
}

fn has_iac_workflow(text: &str) -> bool {
    contains_word(text, "terraform")
        || contains_ros_template(text)
        || contains_template_stack_action(text)
        || text.contains("模板生成")
        || text.contains("模版生成")
        || contains_any_ordered(text, &["生成", "模板"])
        || contains_any_ordered(text, &["生成", "模版"])
        || contains_any_ordered(text, &["编写", "模板"])
        || contains_any_ordered(text, &["编写", "模版"])
        || contains_any_ordered(text, &["写", "模板"])
        || contains_any_ordered(text, &["写", "模版"])
        || contains_any_ordered(text, &["解释", "模板"])
        || contains_any_ordered(text, &["解释", "模版"])
        || contains_any_ordered(text, &["完善", "模板"])
        || contains_any_ordered(text, &["完善", "模版"])
        || contains_any_ordered(text, &["校验", "模板"])
        || contains_any_ordered(text, &["校验", "模版"])
        || contains_any_ordered(text, &["验证", "模板"])
        || contains_any_ordered(text, &["验证", "模版"])
        || contains_any_ordered(text, &["更新", "模板"])
        || contains_any_ordered(text, &["更新", "模版"])
        || contains_any_ordered(text, &["删除", "模板"])
        || contains_any_ordered(text, &["删除", "模版"])
        || text.contains("资源栈")
        || contains_deploy_iac_noun(text)
        || contains_localized_template_action(text)
        || contains_word(text, "createstack")
        || contains_word(text, "validatetemplate")
        || text.contains(".tf")
        || text.contains(".ros.yaml")
        || text.contains(".ros.yml")
}

fn contains_template_stack_action(text: &str) -> bool {
    const ACTIONS: &[&str] = &[
        "create", "generate", "write", "deploy", "explain", "validate", "improve", "update",
        "delete",
    ];
    const OBJECTS: &[&str] = &["template", "stack"];
    ACTIONS.iter().any(|action| {
        OBJECTS.iter().any(|object| {
            contains_words_in_order(text, &[action, object])
                || contains_words_in_order(text, &[object, action])
        })
    })
}

fn contains_deploy_iac_noun(text: &str) -> bool {
    const NOUNS: &[&str] = &["模板", "模版", "资源栈", "ros", "terraform"];
    NOUNS.iter().any(|noun| {
        contains_any_ordered(text, &["部署", noun]) || contains_any_ordered(text, &[noun, "部署"])
    })
}

fn contains_localized_template_action(text: &str) -> bool {
    const GROUPS: &[(&[&str], &[&str])] = &[
        (
            &[
                "genera",
                "generar",
                "crea",
                "crear",
                "despliega",
                "desplegar",
                "explica",
                "explicar",
                "valida",
                "validar",
                "mejora",
                "mejorar",
            ],
            &["plantilla"],
        ),
        (
            &[
                "cree",
                "creer",
                "crée",
                "créer",
                "genere",
                "generer",
                "génère",
                "générer",
                "deploie",
                "deployer",
                "déploie",
                "déployer",
                "explique",
                "expliquer",
                "valide",
                "valider",
                "ameliore",
                "ameliorer",
                "améliore",
                "améliorer",
            ],
            &["modele", "modèle"],
        ),
        (
            &[
                "erstelle",
                "erstellen",
                "generiere",
                "generieren",
                "bereitstelle",
                "bereitstellen",
                "erklaere",
                "erklaeren",
                "erkläre",
                "erklären",
                "validiere",
                "validieren",
                "verbessere",
                "verbessern",
            ],
            &["vorlage"],
        ),
        (
            &[
                "gere",
                "gerar",
                "crie",
                "criar",
                "implante",
                "implantar",
                "explique",
                "explicar",
                "valide",
                "validar",
                "melhore",
                "melhorar",
            ],
            &["modelo"],
        ),
    ];

    GROUPS.iter().any(|(actions, objects)| {
        actions.iter().any(|action| {
            objects.iter().any(|object| {
                contains_words_in_order(text, &[action, object])
                    || contains_words_in_order(text, &[object, action])
            })
        })
    }) || contains_words_in_order(text, &["plantilla", "ros"])
        || contains_words_in_order(text, &["modele", "ros"])
        || contains_words_in_order(text, &["modèle", "ros"])
        || contains_words_in_order(text, &["ros", "vorlage"])
        || contains_words_in_order(text, &["modelo", "ros"])
        || contains_japanese_template_action(text)
}

fn contains_japanese_template_action(text: &str) -> bool {
    const ACTIONS: &[&str] = &[
        "生成",
        "作成",
        "デプロイ",
        "説明",
        "検証",
        "改善",
        "更新",
        "削除",
    ];
    ACTIONS.iter().any(|action| {
        contains_any_ordered(text, &[action, "テンプレート"])
            || contains_any_ordered(text, &["テンプレート", action])
    }) || contains_any_ordered(text, &["ros", "テンプレート"])
}

fn contains_ros_template(text: &str) -> bool {
    contains_words_in_order(text, &["ros", "template"])
        || text.contains("ros-template")
        || contains_any_ordered(text, &["ros", "模板"])
        || contains_any_ordered(text, &["ros", "模版"])
}

fn contains_phrase_words(text: &str, words: &[&str]) -> bool {
    text.split_whitespace()
        .collect::<Vec<_>>()
        .windows(words.len())
        .any(|window| window == words)
}

fn contains_words_in_order(text: &str, words: &[&str]) -> bool {
    let tokens = text
        .split(|ch: char| !ch.is_ascii_alphanumeric() && ch != '_')
        .filter(|token| !token.is_empty())
        .collect::<Vec<_>>();
    tokens.windows(words.len()).any(|window| window == words)
}

fn contains_any_ordered(text: &str, parts: &[&str]) -> bool {
    let mut offset = 0usize;
    for part in parts {
        let Some(index) = text[offset..].find(part) else {
            return false;
        };
        offset += index + part.len();
    }
    true
}

fn contains_word(text: &str, needle: &str) -> bool {
    text.match_indices(needle).any(|(index, _)| {
        let before = text[..index].chars().next_back();
        let after = text[index + needle.len()..].chars().next();
        !before.is_some_and(is_ascii_word_char) && !after.is_some_and(is_ascii_word_char)
    })
}

fn is_ascii_word_char(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || ch == '_'
}

fn context_has_skill_tag(messages: &[AgentMessage], skill_name: &str) -> bool {
    let tag = format!("<skill-name>{skill_name}</skill-name>");
    messages
        .iter()
        .any(|message| message_content_contains(&message.content, &tag))
}

fn message_content_contains(content: &AgentMessageContent, needle: &str) -> bool {
    match content {
        AgentMessageContent::Text(text) => text.contains(needle),
        AgentMessageContent::Blocks(blocks) => blocks.iter().any(|block| match block {
            AgentContentBlock::Text(text) => text.text.contains(needle),
            AgentContentBlock::ToolResult(result) => result.content.contains(needle),
            AgentContentBlock::Thinking(thinking) => thinking.thinking.contains(needle),
            _ => false,
        }),
    }
}
