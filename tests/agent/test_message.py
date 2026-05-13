"""Tests for the message module."""

from iac_code.agent.message import (
    Conversation,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


class TestTextBlock:
    """Tests for TextBlock."""

    def test_create_text_block(self):
        """Test creating a TextBlock."""
        block = TextBlock(text="Hello, world!")
        assert block.type == "text"
        assert block.text == "Hello, world!"

    def test_text_block_serialization(self):
        """Test TextBlock serialization."""
        block = TextBlock(text="Hello")
        data = block.model_dump()
        assert data == {"type": "text", "text": "Hello"}


class TestToolUseBlock:
    """Tests for ToolUseBlock."""

    def test_create_tool_use_block(self):
        """Test creating a ToolUseBlock."""
        block = ToolUseBlock(name="read_file", input={"path": "/tmp/test.txt"})
        assert block.type == "tool_use"
        assert block.name == "read_file"
        assert block.input == {"path": "/tmp/test.txt"}
        # Auto-generated ID should start with "toolu_"
        assert block.id.startswith("toolu_")

    def test_tool_use_block_with_custom_id(self):
        """Test ToolUseBlock with custom ID."""
        block = ToolUseBlock(id="custom_id", name="bash", input={"command": "ls"})
        assert block.id == "custom_id"
        assert block.name == "bash"

    def test_tool_use_block_empty_input(self):
        """Test ToolUseBlock with no input (default empty dict)."""
        block = ToolUseBlock(name="list_files")
        assert block.input == {}


class TestToolResultBlock:
    """Tests for ToolResultBlock."""

    def test_create_tool_result_block(self):
        """Test creating a ToolResultBlock."""
        block = ToolResultBlock(
            tool_use_id="toolu_123",
            content="File contents here",
        )
        assert block.type == "tool_result"
        assert block.tool_use_id == "toolu_123"
        assert block.content == "File contents here"
        assert block.is_error is False

    def test_tool_result_block_with_error(self):
        """Test ToolResultBlock with error flag."""
        block = ToolResultBlock(
            tool_use_id="toolu_456",
            content="File not found",
            is_error=True,
        )
        assert block.is_error is True


class TestMessage:
    """Tests for Message."""

    def test_create_message_with_str_content(self):
        """Test creating a Message with string content."""
        msg = Message(role="user", content="Hello!")
        assert msg.role == "user"
        assert msg.content == "Hello!"

    def test_create_message_with_list_content(self):
        """Test creating a Message with list content."""
        blocks = [
            TextBlock(text="Here's the result:"),
            ToolUseBlock(name="read_file", input={"path": "/test.txt"}),
        ]
        msg = Message(role="assistant", content=blocks)
        assert msg.role == "assistant"
        assert len(msg.content) == 2

    def test_get_text_from_str_content(self):
        """Test get_text() with string content."""
        msg = Message(role="user", content="Simple message")
        assert msg.get_text() == "Simple message"

    def test_get_text_from_list_content(self):
        """Test get_text() with list content containing TextBlocks."""
        blocks = [
            TextBlock(text="First line"),
            ToolUseBlock(name="bash", input={}),
            TextBlock(text="Second line"),
        ]
        msg = Message(role="assistant", content=blocks)
        assert msg.get_text() == "First line\nSecond line"

    def test_get_text_from_list_with_no_text_blocks(self):
        """Test get_text() with list content containing no TextBlocks."""
        blocks = [ToolUseBlock(name="bash", input={})]
        msg = Message(role="assistant", content=blocks)
        assert msg.get_text() == ""

    def test_get_tool_use_blocks_from_str_content(self):
        """Test get_tool_use_blocks() with string content returns empty list."""
        msg = Message(role="user", content="No tools here")
        assert msg.get_tool_use_blocks() == []

    def test_get_tool_use_blocks_from_list_content(self):
        """Test get_tool_use_blocks() extracts tool use blocks."""
        tool_block = ToolUseBlock(name="read_file", input={"path": "/test.txt"})
        blocks = [
            TextBlock(text="Let me read the file"),
            tool_block,
        ]
        msg = Message(role="assistant", content=blocks)
        tool_blocks = msg.get_tool_use_blocks()
        assert len(tool_blocks) == 1
        assert tool_blocks[0].name == "read_file"

    def test_has_tool_use_true(self):
        """Test has_tool_use() returns True when tool use blocks exist."""
        blocks = [ToolUseBlock(name="bash", input={"command": "ls"})]
        msg = Message(role="assistant", content=blocks)
        assert msg.has_tool_use() is True

    def test_has_tool_use_false_str_content(self):
        """Test has_tool_use() returns False for string content."""
        msg = Message(role="user", content="Just text")
        assert msg.has_tool_use() is False

    def test_has_tool_use_false_no_tool_blocks(self):
        """Test has_tool_use() returns False when no tool blocks."""
        blocks = [TextBlock(text="Only text")]
        msg = Message(role="assistant", content=blocks)
        assert msg.has_tool_use() is False

    def test_to_api_format_str_content(self):
        """Test to_api_format() with string content."""
        msg = Message(role="user", content="Hello")
        result = msg.to_api_format()
        assert result == {"role": "user", "content": "Hello"}

    def test_to_api_format_list_content_with_text(self):
        """Test to_api_format() with list content containing TextBlock."""
        msg = Message(role="assistant", content=[TextBlock(text="Hi")])
        result = msg.to_api_format()
        assert result == {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi"}],
        }

    def test_to_api_format_list_content_with_tool_use(self):
        """Test to_api_format() with ToolUseBlock."""
        tool_block = ToolUseBlock(
            id="toolu_abc",
            name="read_file",
            input={"path": "/test.txt"},
        )
        msg = Message(role="assistant", content=[tool_block])
        result = msg.to_api_format()
        assert result == {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "read_file",
                    "input": {"path": "/test.txt"},
                }
            ],
        }

    def test_to_api_format_list_content_with_tool_result(self):
        """Test to_api_format() with ToolResultBlock."""
        result_block = ToolResultBlock(
            tool_use_id="toolu_xyz",
            content="File contents",
            is_error=False,
        )
        msg = Message(role="user", content=[result_block])
        result = msg.to_api_format()
        assert result == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_xyz",
                    "content": "File contents",
                    "is_error": False,
                }
            ],
        }


class TestConversation:
    """Tests for Conversation."""

    def test_create_empty_conversation(self):
        """Test creating an empty Conversation."""
        conv = Conversation()
        assert conv.messages == []

    def test_add_user_message(self):
        """Test add_user_message() adds a user message."""
        conv = Conversation()
        msg = conv.add_user_message("Hello!")
        assert len(conv.messages) == 1
        assert msg.role == "user"
        assert msg.content == "Hello!"
        assert conv.messages[0] is msg

    def test_add_assistant_message_str(self):
        """Test add_assistant_message() with string content."""
        conv = Conversation()
        msg = conv.add_assistant_message("Hi there!")
        assert len(conv.messages) == 1
        assert msg.role == "assistant"
        assert msg.content == "Hi there!"

    def test_add_assistant_message_list(self):
        """Test add_assistant_message() with list content."""
        conv = Conversation()
        blocks = [TextBlock(text="Hello"), ToolUseBlock(name="bash", input={})]
        msg = conv.add_assistant_message(blocks)
        assert msg.role == "assistant"
        assert len(msg.content) == 2

    def test_add_tool_results(self):
        """Test add_tool_results() adds tool results as user message."""
        conv = Conversation()
        results = [
            ToolResultBlock(tool_use_id="toolu_1", content="Result 1"),
            ToolResultBlock(tool_use_id="toolu_2", content="Result 2"),
        ]
        msg = conv.add_tool_results(results)
        assert msg.role == "user"
        assert len(msg.content) == 2

    def test_to_api_format(self):
        """Test to_api_format() converts all messages."""
        conv = Conversation()
        conv.add_user_message("Hi")
        conv.add_assistant_message("Hello!")
        result = conv.to_api_format()
        assert len(result) == 2
        assert result[0] == {"role": "user", "content": "Hi"}
        assert result[1] == {"role": "assistant", "content": "Hello!"}

    def test_to_api_format_empty_conversation(self):
        """Test to_api_format() with empty conversation."""
        conv = Conversation()
        assert conv.to_api_format() == []
