use iac_code_tui::{
    CompletionToken, SkillCatalog, SkillDefinition, SkillSuggestionProvider, SuggestionProvider,
};

#[test]
fn skill_provider_returns_only_skill_suggestions_for_dollar_trigger() {
    let provider = SkillSuggestionProvider::new(sample_skills());

    assert_eq!(provider.trigger(), "$");

    let items = provider.provide(&token("$"));
    let names = display_texts(&items);

    assert_eq!(names, vec!["deploy", "review"]);
    assert!(!names.contains(&"help"));
    assert!(!names.contains(&"model"));
    assert!(items.iter().all(|item| {
        item.source == "skill" && item.icon.as_deref() == Some("$") && item.id.starts_with("skill:")
    }));
}

#[test]
fn skill_provider_omits_disabled_skills_when_they_are_not_registered() {
    let mut catalog = SkillCatalog::new();
    catalog.register(skill("enabled", "Enabled skill", &[]));
    let provider = SkillSuggestionProvider::new(catalog);

    let items = provider.provide(&token("$"));

    assert_eq!(display_texts(&items), vec!["enabled"]);
}

#[test]
fn skill_provider_matches_partial_alias_subsequence_and_description_like_command_registry() {
    let mut catalog = SkillCatalog::new();
    catalog.register(skill("deploy", "Deploy a stack", &["ship"]));
    catalog.register(skill("review", "Review a template", &[]));
    let provider = SkillSuggestionProvider::new(catalog);

    let deploy_items = provider.provide(&token("$dep"));
    let deploy = deploy_items
        .iter()
        .find(|item| item.display_text == "deploy")
        .expect("deploy suggestion should exist");
    assert_eq!(deploy.completion, "$deploy ");
    assert_eq!(deploy.description.as_deref(), Some("Deploy a stack"));

    let alias_items = provider.provide(&token("$shi"));
    let alias = alias_items
        .iter()
        .find(|item| item.display_text == "ship")
        .expect("skill alias suggestion should exist");
    assert_eq!(alias.id, "skill:deploy");
    assert_eq!(alias.completion, "$ship ");

    let subsequence_items = provider.provide(&token("$rvw"));
    assert!(display_texts(&subsequence_items).contains(&"review"));

    let description_items = provider.provide(&token("$template"));
    assert!(display_texts(&description_items).contains(&"review"));

    assert_eq!(provider.provide(&token("$xyzabc")), Vec::new());
}

#[test]
fn skill_provider_handles_mid_sentence_tokens() {
    let provider = SkillSuggestionProvider::new(sample_skills());

    let items = provider.provide(&CompletionToken {
        text: "$rev".to_owned(),
        start: 6,
        end: 10,
        trigger: "$".to_owned(),
    });

    assert!(display_texts(&items).contains(&"review"));
}

fn sample_skills() -> SkillCatalog {
    let mut catalog = SkillCatalog::new();
    catalog.register(skill("deploy", "Deploy a stack", &[]));
    catalog.register(skill("review", "Review a template", &[]));
    catalog
}

fn skill(name: &str, description: &str, aliases: &[&str]) -> SkillDefinition {
    SkillDefinition {
        name: name.to_owned(),
        description: description.to_owned(),
        aliases: aliases.iter().map(|alias| (*alias).to_owned()).collect(),
        hidden: false,
    }
}

fn token(text: &str) -> CompletionToken {
    CompletionToken {
        text: text.to_owned(),
        start: 0,
        end: text.len(),
        trigger: "$".to_owned(),
    }
}

fn display_texts(items: &[iac_code_tui::SuggestionItem]) -> Vec<&str> {
    items
        .iter()
        .map(|item| item.display_text.as_str())
        .collect()
}
