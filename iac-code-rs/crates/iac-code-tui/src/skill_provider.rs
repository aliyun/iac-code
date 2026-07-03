use std::cmp::Ordering;

use crate::{CompletionToken, SuggestionItem, SuggestionProvider};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SkillDefinition {
    pub name: String,
    pub description: String,
    pub aliases: Vec<String>,
    pub hidden: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SkillFuzzyMatch {
    pub skill: SkillDefinition,
    pub name: String,
    pub priority: u8,
    pub score: f64,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SkillCatalog {
    skills: Vec<SkillDefinition>,
}

impl SkillCatalog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, skill: SkillDefinition) {
        if let Some(existing) = self
            .skills
            .iter_mut()
            .find(|existing| existing.name == skill.name)
        {
            *existing = skill;
            return;
        }
        self.skills.push(skill);
    }

    pub fn get_all(&self) -> Vec<SkillDefinition> {
        let mut skills = self
            .skills
            .iter()
            .filter(|skill| !skill.hidden)
            .cloned()
            .collect::<Vec<_>>();
        skills.sort_by(|left, right| left.name.cmp(&right.name));
        skills
    }

    pub fn fuzzy_search(&self, query: &str) -> Vec<SkillFuzzyMatch> {
        if query.is_empty() {
            return self
                .get_all()
                .into_iter()
                .map(|skill| SkillFuzzyMatch {
                    name: skill.name.clone(),
                    skill,
                    priority: 0,
                    score: 0.0,
                })
                .collect();
        }

        let query_lower = query.to_lowercase();
        let mut matches = Vec::new();

        for skill in self.get_all() {
            let name_lower = skill.name.to_lowercase();
            if name_lower == query_lower {
                matches.push(SkillFuzzyMatch {
                    name: skill.name.clone(),
                    skill,
                    priority: 0,
                    score: 0.0,
                });
                continue;
            }

            if name_lower.starts_with(&query_lower) {
                matches.push(SkillFuzzyMatch {
                    score: skill.name.chars().count() as f64,
                    name: skill.name.clone(),
                    skill,
                    priority: 1,
                });
                continue;
            }

            let mut alias_match = None;
            for alias in &skill.aliases {
                let alias_lower = alias.to_lowercase();
                if alias_lower == query_lower {
                    alias_match = Some((alias.clone(), 2, 0.0));
                    break;
                }
                if alias_lower.starts_with(&query_lower) {
                    alias_match = Some((alias.clone(), 3, alias.chars().count() as f64));
                    break;
                }
            }
            if let Some((name, priority, score)) = alias_match {
                matches.push(SkillFuzzyMatch {
                    skill,
                    name,
                    priority,
                    score,
                });
                continue;
            }

            if let Some(score) = subsequence_score(query, &skill.name) {
                matches.push(SkillFuzzyMatch {
                    name: skill.name.clone(),
                    skill,
                    priority: 4,
                    score,
                });
                continue;
            }

            let description_lower = skill.description.to_lowercase();
            if let Some(index) = description_lower.find(&query_lower) {
                matches.push(SkillFuzzyMatch {
                    name: skill.name.clone(),
                    skill,
                    priority: 5,
                    score: index as f64,
                });
            }
        }

        matches.sort_by(|left, right| {
            left.priority.cmp(&right.priority).then_with(|| {
                left.score
                    .partial_cmp(&right.score)
                    .unwrap_or(Ordering::Equal)
            })
        });
        matches
    }
}

pub struct SkillSuggestionProvider {
    catalog: SkillCatalog,
}

impl SkillSuggestionProvider {
    pub fn new(catalog: SkillCatalog) -> Self {
        Self { catalog }
    }
}

impl SuggestionProvider for SkillSuggestionProvider {
    fn trigger(&self) -> &str {
        "$"
    }

    fn provide(&self, token: &CompletionToken) -> Vec<SuggestionItem> {
        let query = token.text.strip_prefix('$').unwrap_or(&token.text);
        self.catalog
            .fuzzy_search(query)
            .into_iter()
            .map(|match_| SuggestionItem {
                id: format!("skill:{}", match_.skill.name),
                display_text: match_.name.clone(),
                completion: format!("${} ", match_.name),
                description: Some(match_.skill.description),
                icon: Some("$".to_owned()),
                source: "skill".to_owned(),
                score: -(match_.priority as f64) * 1000.0 - match_.score,
                arg_hint: None,
            })
            .collect()
    }
}

fn subsequence_score(query: &str, target: &str) -> Option<f64> {
    let query_chars = query.to_lowercase().chars().collect::<Vec<_>>();
    let target_chars = target.to_lowercase().chars().collect::<Vec<_>>();
    let mut query_index = 0;
    let mut positions = Vec::new();

    for (target_index, ch) in target_chars.iter().copied().enumerate() {
        if query_index < query_chars.len() && ch == query_chars[query_index] {
            positions.push(target_index);
            query_index += 1;
        }
    }

    if query_index < query_chars.len() {
        return None;
    }

    let gap_penalty = positions
        .windows(2)
        .map(|pair| pair[1] - pair[0] - 1)
        .sum::<usize>();
    let start_penalty = positions.first().copied().unwrap_or_default();
    Some((gap_penalty + start_penalty) as f64)
}
