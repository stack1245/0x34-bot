from __future__ import annotations

import logging
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import discord
from discord.commands import SlashCommandGroup

logger = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).resolve().parent
INTERNAL_MODULE_STEMS = {"__init__", "group_config", "support"}
MOUNTED_MODULES: dict[str, ModuleType] = {}
GROUP_CONFIG = import_module(".group_config", __name__)
SUPPORT_MODULE = import_module(".support", __name__)
command_group = SlashCommandGroup(
    GROUP_CONFIG.COMMAND_GROUP_NAME,
    GROUP_CONFIG.COMMAND_GROUP_DESCRIPTION,
)
globals()[GROUP_CONFIG.GROUP_VARIABLE_NAME] = command_group

for export_name in getattr(SUPPORT_MODULE, "__all__", ()):
    globals()[export_name] = getattr(SUPPORT_MODULE, export_name)


def _iter_subcommand_module_names() -> list[str]:
    module_names: list[str] = []
    for path in sorted(PACKAGE_DIR.iterdir(), key=lambda entry: entry.name):
        if not path.is_file() or path.suffix != ".py":
            continue

        if path.stem.startswith("_") or path.stem in INTERNAL_MODULE_STEMS:
            continue

        module_names.append(path.stem)

    return module_names


def _mount_subcommand_modules(bot: discord.Bot) -> None:
    for module_name in _iter_subcommand_module_names():
        module = MOUNTED_MODULES.get(module_name)
        if module is None:
            module = import_module(f".{module_name}", __name__)
            MOUNTED_MODULES[module_name] = module
            logger.info(
                "Imported subcommand module. package=%s module=%s",
                __name__,
                module.__name__,
            )

        module_setup = getattr(module, "setup", None)
        if callable(module_setup):
            logger.info(
                "Mounting subcommand module. package=%s module=%s",
                __name__,
                module.__name__,
            )
            module_setup(bot)


def setup(bot: discord.Bot) -> None:
    _mount_subcommand_modules(bot)
    logger.info(
        "Registering slash command group. package=%s group=%s",
        __name__,
        GROUP_CONFIG.COMMAND_GROUP_NAME,
    )
    bot.add_application_command(command_group)


def __getattr__(name: str) -> Any:
    if hasattr(SUPPORT_MODULE, name):
        return getattr(SUPPORT_MODULE, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "command_group",
    GROUP_CONFIG.GROUP_VARIABLE_NAME,
    *getattr(SUPPORT_MODULE, "__all__", ()),
]
