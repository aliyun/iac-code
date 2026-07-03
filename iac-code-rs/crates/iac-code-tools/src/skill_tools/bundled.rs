use std::fs;
use std::path::{Component, Path, PathBuf};

use super::{SkillDefinition, SkillSource};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct BundledSkillFile {
    relative_path: &'static str,
    contents: &'static str,
}

const IAC_ALIYUN_SKILL_MD: &str =
    include_str!("../../../../resources/skills/bundled/iac_aliyun/SKILL.md");

const IAC_ALIYUN_BUNDLED_FILES: &[BundledSkillFile] = &[
    BundledSkillFile {
        relative_path: "SKILL.md",
        contents: IAC_ALIYUN_SKILL_MD,
    },
    BundledSkillFile {
        relative_path: "auto_trigger.py",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/auto_trigger.py"
        ),
    },
    BundledSkillFile {
        relative_path: "references/template-parameters.md",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/references/template-parameters.md"
        ),
    },
    BundledSkillFile {
        relative_path: "references/template-parameter-recommendation.md",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/references/template-parameter-recommendation.md"
        ),
    },
    BundledSkillFile {
        relative_path: "references/ros-template.md",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/references/ros-template.md"
        ),
    },
    BundledSkillFile {
        relative_path: "references/terraform-template.md",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/references/terraform-template.md"
        ),
    },
    BundledSkillFile {
        relative_path: "references/cloud-products/ecs.md",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/references/cloud-products/ecs.md"
        ),
    },
    BundledSkillFile {
        relative_path: "references/cloud-products/oss.md",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/references/cloud-products/oss.md"
        ),
    },
    BundledSkillFile {
        relative_path: "references/cloud-products/rds.md",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/references/cloud-products/rds.md"
        ),
    },
    BundledSkillFile {
        relative_path: "references/cloud-products/redis.md",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/references/cloud-products/redis.md"
        ),
    },
    BundledSkillFile {
        relative_path: "references/cloud-products/slb.md",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/references/cloud-products/slb.md"
        ),
    },
    BundledSkillFile {
        relative_path: "references/cloud-products/vpc.md",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/references/cloud-products/vpc.md"
        ),
    },
    BundledSkillFile {
        relative_path: "scripts/tf2ros.py",
        contents: include_str!(
            "../../../../resources/skills/bundled/iac_aliyun/scripts/tf2ros.py"
        ),
    },
];

pub(super) fn bundled_skills(user_skills_dir: &Path) -> Result<Vec<SkillDefinition>, String> {
    Ok(vec![simplify_skill(), iac_aliyun_skill(user_skills_dir)?])
}

fn simplify_skill() -> SkillDefinition {
    SkillDefinition {
        name: "simplify".into(),
        description: "Review changed code for reuse, quality, and efficiency, then fix issues found."
            .into(),
        allowed_tools: Vec::new(),
        when_to_use: String::new(),
        arguments: Vec::new(),
        content: "Review the recently changed code for:\n\n1. **Reuse** — Are there existing functions, utilities, or patterns in the codebase that could replace newly added code? Search broadly.\n2. **Quality** — Are there bugs, edge cases, or logic errors?\n3. **Efficiency** — Can the code be simplified without losing clarity?\n\nFor each issue found:\n- Explain the problem\n- Show the fix (edit the file directly)\n\nIf no issues are found, say so briefly.\n".into(),
        source: SkillSource::Bundled,
        file_path: String::new(),
        skill_root: String::new(),
        user_invocable: true,
        model_override: "inherit".into(),
        effort_override: String::new(),
        context: "inline".into(),
        agent: "general-purpose".into(),
    }
}

fn iac_aliyun_skill(user_skills_dir: &Path) -> Result<SkillDefinition, String> {
    let skill_root = bundled_skill_cache_root(user_skills_dir).join("iac-aliyun");
    materialize_bundled_skill_files(&skill_root, IAC_ALIYUN_BUNDLED_FILES)?;
    Ok(SkillDefinition {
        name: "iac-aliyun".into(),
        description: "阿里云 Alibaba Cloud ROS/Terraform IaC 模板生成、解释、完善、校验、询价与部署".into(),
        allowed_tools: Vec::new(),
        when_to_use: "当用户请求阿里云/Alibaba Cloud/Alicloud 的 ROS 模板、资源栈、Terraform alicloud provider 模板生成、解释、完善、校验、询价、部署、更新或删除时，必须先调用 skill 工具加载 iac-aliyun。".into(),
        arguments: Vec::new(),
        content: IAC_ALIYUN_SKILL_MD.into(),
        source: SkillSource::Bundled,
        file_path: skill_root.join("SKILL.md").to_string_lossy().into_owned(),
        skill_root: skill_root.to_string_lossy().into_owned(),
        user_invocable: false,
        model_override: "inherit".into(),
        effort_override: String::new(),
        context: "inline".into(),
        agent: "general-purpose".into(),
    })
}

fn bundled_skill_cache_root(user_skills_dir: &Path) -> PathBuf {
    user_skills_dir
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
        .join("bundled-skills")
}

fn materialize_bundled_skill_files(
    skill_root: &Path,
    files: &[BundledSkillFile],
) -> Result<(), String> {
    for file in files {
        let relative_path = Path::new(file.relative_path);
        if !is_safe_bundled_relative_path(relative_path) {
            return Err(format!(
                "unsafe bundled skill resource path: {}",
                file.relative_path
            ));
        }
        let destination = skill_root.join(relative_path);
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        if fs::read_to_string(&destination)
            .ok()
            .as_deref()
            .is_some_and(|existing| existing == file.contents)
        {
            continue;
        }
        fs::write(&destination, file.contents).map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn is_safe_bundled_relative_path(path: &Path) -> bool {
    !path.as_os_str().is_empty()
        && path.components().all(|component| {
            matches!(component, Component::Normal(_)) || matches!(component, Component::CurDir)
        })
}
