"""
LLM Configuration Module
Manages API configurations for multiple LLM providers.
"""
from typing import Dict, Any
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


# ===== CHANGE PROVIDER HERE =====
SELECTED_PROVIDER = "claude" # or "gpt4o-mini", "claude", "gpt4o" etc.
# ================================


class LLMConfig:
    """Configuration class for LLM providers"""

    ANTHROPIC_PROXY_URL = os.getenv("ANTHROPIC_PROXY_URL")
    API_KEY_ENV_VARS = {
        "gpt4o-mini": "GPT4O_MINI_API_KEY",
        "gpt4o": "GPT4O_API_KEY",
        "groq": "GROQ_API_KEY",
        "claude": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }

    # API Endpoints
    ENDPOINTS = {
        "gpt4o-mini": "https://api2.aigcbest.top/v1/chat/completions",
        "gpt4o":      "https://api2.aigcbest.top/v1/chat/completions",
        "groq":       "https://api.groq.com/openai/v1/chat/completions",
        "claude":     ANTHROPIC_PROXY_URL or "https://api.anthropic.com/v1/messages",
        "gemini":     "https://generativelanguage.googleapis.com/v1beta/models"
    }

    # API Keys
    API_KEYS = {
        "gpt4o-mini": os.getenv("GPT4O_MINI_API_KEY"),
        "gpt4o":      os.getenv("GPT4O_API_KEY"),
        "groq":       os.getenv("GROQ_API_KEY"),
        "claude":     os.getenv("ANTHROPIC_API_KEY"),
        "gemini":     os.getenv("GEMINI_API_KEY"),
    }

    # Model Names
    MODELS = {
        "gpt4o-mini": "gpt-4o-mini",
        "gpt4o":      "gpt-4o",
        "groq":       "openai/gpt-oss-120b",
#        "claude":     "claude-haiku-4-5-20251001",
        "claude":     "claude-sonnet-4-6",
        "gemini":     "gemini-2.0-flash-exp"
    }

    # Supported providers
    SUPPORTED_PROVIDERS = ["gpt4o-mini", "gpt4o", "groq", "claude", "gemini"]

    @classmethod
    def get_config(cls, provider: str) -> Dict[str, Any]:
        """
        Get configuration for a specific LLM provider.

        Args:
            provider: Name of the LLM provider
        Returns:
            Dictionary containing api_url, api_key, and model_name
        Raises:
            ValueError: If provider is not supported or key is missing
        """
        provider = provider.lower()

        if provider not in cls.SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unsupported provider: {provider}. "
                f"Supported: {', '.join(cls.SUPPORTED_PROVIDERS)}"
            )

        api_key = cls.API_KEYS.get(provider)
        if not api_key:
            env_var = cls.API_KEY_ENV_VARS[provider]
            raise ValueError(
                f"API key not found for provider: {provider}. "
                f"Please set environment variable {env_var}."
            )

        return {
            "provider":   provider,
            "api_url":    cls.ENDPOINTS[provider],
            "api_key":    api_key,
            "model_name": cls.MODELS[provider]
        }

    @classmethod
    def get_selected_config(cls) -> Dict[str, Any]:
        """Shortcut — returns config for the globally selected provider."""
        return cls.get_config(SELECTED_PROVIDER)

    @classmethod
    def list_providers(cls) -> list:
        """List all supported providers."""
        return cls.SUPPORTED_PROVIDERS.copy()

    @classmethod
    def validate_provider(cls, provider: str) -> bool:
        """Check if a provider is supported."""
        return provider.lower() in cls.SUPPORTED_PROVIDERS


# ===== Example usage =====
if __name__ == "__main__":
    print(f"Active provider : {SELECTED_PROVIDER}")
    print(f"All providers   : {LLMConfig.list_providers()}\n")

    config = LLMConfig.get_selected_config()
    print(f"Model   : {config['model_name']}")
    print(f"API URL : {config['api_url']}")
    print(f"API Key : {config['api_key'][:20]}...")
