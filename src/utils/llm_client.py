"""
LLM client wrapper for Claude API (Anthropic).

모든 에이전트 판단 로직이 이 모듈을 통해 Claude에 위임된다.
ANTHROPIC_API_KEY 환경변수가 필요하다.

제공 함수:
  ask_with_image(prompt, image_path) → str
  ask_text(prompt)                   → str
  ask_json(prompt, image_path?)      → dict   ← 주 사용 함수
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Dict, Optional

import anthropic

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024

# 모듈 레벨 클라이언트 (lazy init)
_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY 환경변수가 설정되어 있지 않습니다.\n"
                "  export ANTHROPIC_API_KEY=sk-ant-..."
            )
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def ask_with_image(prompt: str, image_path: str) -> str:
    """
    이미지와 텍스트 프롬프트를 Claude Vision에 전송하고 응답 텍스트를 반환한다.

    Args:
        prompt:     텍스트 프롬프트
        image_path: PNG/JPEG 이미지 경로

    Returns:
        Claude의 응답 텍스트
    """
    with open(image_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    ext = os.path.splitext(image_path)[1].lower()
    media_type = "image/png" if ext == ".png" else "image/jpeg"

    client = _get_client()
    message = client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return message.content[0].text


def ask_text(prompt: str) -> str:
    """
    텍스트 전용 프롬프트를 Claude에 전송하고 응답 텍스트를 반환한다.
    """
    client = _get_client()
    message = client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def ask_json(
    prompt: str,
    image_path: Optional[str] = None,
    fallback: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Claude에 프롬프트를 전송하고 JSON 응답을 파싱해서 반환한다.

    Args:
        prompt:     JSON 형식 응답을 요청하는 프롬프트
        image_path: 이미지 첨부 시 경로 (None이면 텍스트 전용)
        fallback:   파싱 실패 시 반환할 기본값 dict

    Returns:
        파싱된 dict. 실패 시 fallback 반환 (fallback도 None이면 예외).
    """
    try:
        if image_path and os.path.exists(image_path):
            raw = ask_with_image(prompt, image_path)
        else:
            raw = ask_text(prompt)

        return _parse_json(raw)

    except Exception as exc:
        if fallback is not None:
            return fallback
        raise RuntimeError(f"LLM 응답 파싱 실패: {exc}\n응답: {raw!r}") from exc


def _parse_json(text: str) -> Dict[str, Any]:
    """응답 텍스트에서 JSON 블록을 추출하고 파싱한다."""
    # ```json ... ``` 코드 블록 우선 추출
    code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if code_block:
        return json.loads(code_block.group(1))

    # 첫 번째 { ... } 블록 추출
    brace_block = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_block:
        return json.loads(brace_block.group())

    # 전체가 JSON인 경우
    return json.loads(text.strip())
