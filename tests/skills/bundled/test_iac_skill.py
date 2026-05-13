from iac_code.skills.bundled import get_bundled_skills, init_bundled_skills


class TestIacSkill:
    def test_iac_skill_registered(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        iac_skills = [s for s in skills if s.name == "iac-aliyun"]
        assert len(iac_skills) == 1

    def test_iac_skill_not_user_invocable(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        iac_skill = next(s for s in skills if s.name == "iac-aliyun")
        assert iac_skill.is_user_invocable is False

    def test_iac_skill_has_description(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        iac_skill = next(s for s in skills if s.name == "iac-aliyun")
        assert len(iac_skill.description) > 0
