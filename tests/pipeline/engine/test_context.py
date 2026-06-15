import time

from iac_code.pipeline.engine.context import PipelineContext, VersionedField

SELLING_DEPS = {
    "intent": [],
    "architecture": ["intent"],
    "selected_plan": ["architecture"],
    "specs": ["selected_plan"],
    "template": ["specs", "selected_plan"],
    "review": ["template"],
    "cost": ["template", "specs"],
    "deployment": ["template"],
}


class TestVersionedField:
    def test_defaults(self):
        f = VersionedField()
        assert f.value is None
        assert f.version == 0
        assert f.stale is False
        assert f.updated_at is None
        assert f.history == []


class TestPipelineContextSetGet:
    def setup_method(self):
        self.ctx = PipelineContext(SELLING_DEPS)

    def test_initial_state_all_none(self):
        for name in SELLING_DEPS:
            assert self.ctx.get_conclusion(name) is None

    def test_set_and_get(self):
        self.ctx.set_conclusion("intent", {"type": "e-commerce"})
        assert self.ctx.get_conclusion("intent") == {"type": "e-commerce"}

    def test_set_increments_version(self):
        self.ctx.set_conclusion("intent", {"v": 1})
        assert self.ctx.get_field("intent").version == 1
        self.ctx.set_conclusion("intent", {"v": 2})
        assert self.ctx.get_field("intent").version == 2

    def test_set_records_history(self):
        self.ctx.set_conclusion("intent", {"v": 1})
        self.ctx.set_conclusion("intent", {"v": 2})
        field = self.ctx.get_field("intent")
        assert len(field.history) == 1
        assert field.history[0]["value"] == {"v": 1}
        assert field.history[0]["version"] == 1

    def test_set_updates_timestamp(self):
        before = time.time()
        self.ctx.set_conclusion("intent", {"v": 1})
        after = time.time()
        t = self.ctx.get_field("intent").updated_at
        assert before <= t <= after


class TestStalePropagation:
    def setup_method(self):
        self.ctx = PipelineContext(SELLING_DEPS)
        self.ctx.set_conclusion("intent", {"type": "e-commerce"})
        self.ctx.set_conclusion("architecture", {"plans": [1, 2]})
        self.ctx.set_conclusion("selected_plan", {"plan": 1})
        self.ctx.set_conclusion("specs", {"cpu": "4c8g"})
        self.ctx.set_conclusion("template", {"ros": "..."})

    def test_updating_intent_stales_all_downstream(self):
        stale = self.ctx.set_conclusion("intent", {"type": "blog"})
        assert "architecture" in stale
        assert "selected_plan" in stale
        assert "specs" in stale
        assert "template" in stale

    def test_updating_selected_plan_stales_specs_and_template(self):
        stale = self.ctx.set_conclusion("selected_plan", {"plan": 2})
        assert "specs" in stale
        assert "template" in stale
        assert "architecture" not in stale

    def test_mark_stale_propagates(self):
        stale = self.ctx.mark_stale("architecture")
        assert self.ctx.get_field("architecture").stale is True
        assert "selected_plan" in stale
        assert "specs" in stale

    def test_clear_stale(self):
        self.ctx.mark_stale("architecture")
        self.ctx.clear_stale("architecture")
        assert self.ctx.get_field("architecture").stale is False

    def test_get_stale_fields(self):
        self.ctx.mark_stale("architecture")
        stale_fields = self.ctx.get_stale_fields()
        assert "architecture" in stale_fields
        assert "selected_plan" in stale_fields

    def test_no_stale_for_unset_downstream(self):
        ctx = PipelineContext(SELLING_DEPS)
        ctx.set_conclusion("intent", {"type": "e-commerce"})
        stale = ctx.set_conclusion("intent", {"type": "blog"})
        assert stale == []


class TestConclusionsSummary:
    def test_summary_excludes_unset(self):
        ctx = PipelineContext(SELLING_DEPS)
        ctx.set_conclusion("intent", {"type": "e-commerce"})
        summary = ctx.get_conclusions_summary()
        assert "intent" in summary
        assert "architecture" not in summary

    def test_summary_includes_stale_flag(self):
        ctx = PipelineContext(SELLING_DEPS)
        ctx.set_conclusion("intent", {"type": "e-commerce"})
        ctx.set_conclusion("architecture", {"plans": [1]})
        ctx.set_conclusion("intent", {"type": "blog"})
        summary = ctx.get_conclusions_summary()
        assert summary["architecture"]["stale"] is True


class TestSnapshot:
    def test_roundtrip(self):
        ctx = PipelineContext(SELLING_DEPS)
        ctx.set_conclusion("intent", {"type": "e-commerce"})
        ctx.set_conclusion("architecture", {"plans": [1, 2]})

        snapshot = ctx.to_snapshot()
        restored = PipelineContext.from_snapshot(snapshot, SELLING_DEPS)

        assert restored.get_conclusion("intent") == {"type": "e-commerce"}
        assert restored.get_conclusion("architecture") == {"plans": [1, 2]}
        assert restored.get_field("intent").version == 1
