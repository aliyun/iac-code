import asyncio
from dataclasses import dataclass

import pytest

from iac_code.i18n import SUPPORTED_LANGUAGES
from iac_code.providers.base import ContentBlock
from iac_code.web import session_titler
from iac_code.web.runtime import WebModelSelection


@dataclass
class _FakeResponse:
    text: str


class _FakeManager:
    """记录构造参数并按预设脚本响应 complete()。"""

    last_kwargs: dict = {}
    last_system = ""

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self._script = _SCRIPT.pop(0) if _SCRIPT else ("ok", "默认标题")

    async def complete(self, messages, system, max_tokens=8192, **_):
        type(self).last_system = system
        kind, value = self._script
        if kind == "raise":
            raise RuntimeError(value)
        if kind == "timeout":
            await asyncio.sleep(value)  # 触发 wait_for 超时
        return _FakeResponse(text=value)


_SCRIPT: list = []


@pytest.fixture(autouse=True)
def _patch_manager(monkeypatch):
    _SCRIPT.clear()
    monkeypatch.setattr(session_titler, "ProviderManager", _FakeManager)
    monkeypatch.setattr(session_titler, "load_credentials", lambda model=None: {})
    yield
    _SCRIPT.clear()


def _selection() -> WebModelSelection:
    return WebModelSelection(provider="dashscope", model="qwen3.7-max", effort=None)


@pytest.mark.asyncio
async def test_generate_title_from_text_success():
    _SCRIPT.append(("ok", "  创建 OSS 存储桶  "))
    title = await session_titler.generate_session_title(
        text="帮我创建一个 OSS 存储桶", image_blocks=[], selection=_selection(), language="en"
    )
    assert title == "创建 OSS 存储桶"
    assert "Write the title in English" in _FakeManager.last_system
    assert "regardless of the language of the input" in _FakeManager.last_system
    # thinking 关闭：effort_override 必须传入（"none" 或最低 effort），且用会话模型
    assert _FakeManager.last_kwargs["model"] == "qwen3.7-max"
    assert _FakeManager.last_kwargs["effort_override"] is not None


@pytest.mark.asyncio
async def test_generate_title_retries_once_then_succeeds():
    _SCRIPT.extend([("raise", "boom"), ("ok", "重试成功标题")])
    title = await session_titler.generate_session_title(
        text="第一条", image_blocks=[], selection=_selection(), language="zh"
    )
    assert title == "重试成功标题"


@pytest.mark.asyncio
async def test_generate_title_returns_none_after_two_failures():
    _SCRIPT.extend([("raise", "boom1"), ("raise", "boom2")])
    title = await session_titler.generate_session_title(
        text="第一条", image_blocks=[], selection=_selection(), language="zh"
    )
    assert title is None


@pytest.mark.asyncio
async def test_generate_title_returns_none_for_blank_response():
    _SCRIPT.append(("ok", "   "))
    title = await session_titler.generate_session_title(
        text="第一条", image_blocks=[], selection=_selection(), language="zh"
    )
    assert title is None


@pytest.mark.asyncio
async def test_generate_title_from_image_only_builds_image_message():
    _SCRIPT.append(("ok", "架构图会话"))
    block = ContentBlock(type="image", media_type="image/png", data="AAAA")
    title = await session_titler.generate_session_title(
        text=None, image_blocks=[block], selection=_selection(), language="ja"
    )
    assert title == "架构图会话"


def test_title_system_prompt_supports_all_web_languages():
    expected = {
        "en": "English",
        "zh": "Simplified Chinese",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "ja": "Japanese",
        "pt": "Portuguese",
    }

    assert set(expected) == set(SUPPORTED_LANGUAGES)
    for language, name in expected.items():
        assert "Write the title in {}".format(name) in session_titler._title_system_prompt(language)
