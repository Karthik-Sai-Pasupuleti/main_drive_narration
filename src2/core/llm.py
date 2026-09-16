"""Model access for the layers: builds an LLMBot from a config section.

Thin wrapper over ``src/bot/bot.py`` so the layers never touch sys.path or the
provider details. ``make_agent(..., enabled=False)`` returns None, which is what
``--dry-run`` uses: each layer then falls back to a templated line and the
timeline, folders, text files and WAVs can be exercised without a model loaded.
"""
from __future__ import annotations

import logging

from core import srcpath  # noqa: F401  # puts ../src on sys.path for the imports below
from bot.bot import AgentConfig, LLMBot        # noqa: E402  (needs srcpath first)
from utils.utils import load_config, load_env  # noqa: E402

__all__ = ["AgentConfig", "LLMBot", "load_config", "load_env", "make_agent", "model_label"]

logger = logging.getLogger(__name__)


def make_agent(cfg: dict, output_model, prompts_dir, enabled: bool = True) -> LLMBot | None:
    """Build the agent described by a `[layer*]` config section.

    Args:
        cfg (dict): the section's keys (provider / model / temperature / prompt / vision).
        output_model (type): the Pydantic schema the model must return as JSON.
        prompts_dir (str | Path): base directory the section's `prompt` path resolves against.
        enabled (bool, optional): False returns None (dry run, no model is contacted).

    Returns:
        LLMBot | None: the configured agent, or None when disabled.
    """
    if not enabled:
        return None
    fields = {k: v for k, v in cfg.items() if k in AgentConfig.model_fields}
    agent_cfg = AgentConfig(**fields)
    logger.info("agent: %s/%s (vision=%s) <- %s", agent_cfg.provider, agent_cfg.model,
                agent_cfg.vision, agent_cfg.prompt)
    return LLMBot(agent_cfg, output_model, str(prompts_dir))


def model_label(cfg: dict, enabled: bool = True) -> str:
    """Return "provider/model" for the run log, or "dry-run" when no model is used."""
    if not enabled:
        return "dry-run"
    return f"{cfg.get('provider', 'ollama')}/{cfg.get('model', '?')}"
