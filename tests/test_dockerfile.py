from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pins():
    text = (ROOT / "Dockerfile").read_text()
    assert "ARG CLAUDE_CODE_VERSION=2.1.304" in text
    assert "node:24.22.0-bookworm-slim" in text
    assert "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in text
    assert "USER 1000" in text and 'ENTRYPOINT ["usage"]' in text
