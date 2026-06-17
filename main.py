from __future__ import annotations

import logging
import shutil
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=SyntaxWarning)

from core.config import BASE_DIR, configure_logging, get_token

configure_logging()

from core.bot import ZeroXThirtyFourBot

logger = logging.getLogger(__name__)


def clean_workspace_cache(root_path: Path) -> None:
    removed_directory_count = 0
    removed_file_count = 0

    for current_path in root_path.rglob("*"):
        if current_path.name == "__pycache__" and current_path.is_dir():
            try:
                shutil.rmtree(current_path)
                removed_directory_count += 1
            except (FileNotFoundError, PermissionError, OSError):
                continue

        if current_path.suffix not in {".pyc", ".pyo"} or not current_path.is_file():
            continue

        try:
            current_path.unlink()
            removed_file_count += 1
        except (FileNotFoundError, PermissionError, OSError):
            continue

    logger.warning(
        "Workspace cache cleanup finished. removed_pycache_dirs=%s removed_bytecode_files=%s",
        removed_directory_count,
        removed_file_count,
    )


def main() -> None:
    clean_workspace_cache(BASE_DIR)
    token = get_token()
    logger.info("Bootstrapping Discord bot runtime.")
    bot = ZeroXThirtyFourBot()
    bot.run(token)


if __name__ == "__main__":
    main()
