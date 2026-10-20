"""OpenAI 兼容 LLM 客户端（httpx 异步）。

支持任意 OpenAI 兼容服务: DeepSeek / 通义千问 / 智谱 / OpenAI / Gemini 等，
通过配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 切换，不绑定单一厂商。

安全: API Key 只从环境变量/配置读取，不落库、不出现在任何接口响应中。
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger("sas.llm")


class LLMClient:
    """OpenAI 兼容聊天客户端。"""

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def available(self) -> bool:
        """是否具备调用条件（开关打开且配置了 Key）。"""
        return self.settings.llm_enabled and bool(self.settings.llm_api_key)

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 1500,
    ) -> str:
        """发送聊天请求，返回助手文本内容。

        Raises:
            RuntimeError: 未配置或调用失败（由上层降级处理）。
        """
        if not self.available:
            raise RuntimeError("LLM 未配置（llm_enabled=False 或缺少 api_key）")

        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - 统一转为 RuntimeError 供上层降级
            logger.warning("LLM 调用失败: %s", exc)
            raise RuntimeError(f"LLM 调用失败: {exc}") from exc


# 全局单例
llm_client = LLMClient()
