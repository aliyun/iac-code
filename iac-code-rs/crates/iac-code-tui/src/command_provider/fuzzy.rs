use std::cmp::Ordering;

use super::{CommandCatalog, FuzzyMatch};

impl CommandCatalog {
    pub fn fuzzy_search(&self, query: &str) -> Vec<FuzzyMatch> {
        if query.is_empty() {
            return self
                .get_all()
                .into_iter()
                .map(|command| FuzzyMatch {
                    name: command.name.clone(),
                    command,
                    priority: 0,
                    score: 0.0,
                })
                .collect();
        }

        let query_lower = query.to_lowercase();
        let mut matches = Vec::new();

        for command in self.get_all() {
            let name_lower = command.name.to_lowercase();
            if name_lower == query_lower {
                matches.push(FuzzyMatch {
                    name: command.name.clone(),
                    command,
                    priority: 0,
                    score: 0.0,
                });
                continue;
            }

            if name_lower.starts_with(&query_lower) {
                matches.push(FuzzyMatch {
                    score: command.name.chars().count() as f64,
                    name: command.name.clone(),
                    command,
                    priority: 1,
                });
                continue;
            }

            let mut alias_match = None;
            for alias in &command.aliases {
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
                matches.push(FuzzyMatch {
                    command,
                    name,
                    priority,
                    score,
                });
                continue;
            }

            if let Some(score) = subsequence_score(query, &command.name) {
                matches.push(FuzzyMatch {
                    name: command.name.clone(),
                    command,
                    priority: 4,
                    score,
                });
                continue;
            }

            let description_lower = command.description.to_lowercase();
            if let Some(index) = description_lower.find(&query_lower) {
                matches.push(FuzzyMatch {
                    name: command.name.clone(),
                    command,
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
