"""Small bot state kept between restarts (opened menu panels, IR modes)."""

import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Final

from hikcamerabot.config.store import CONFIGS_DIR_PATH

# Lives next to the configs: this directory is mounted as a Docker volume,
# so the file survives container rebuilds.
STATE_FILE_PATH: Final[Path] = CONFIGS_DIR_PATH / '.bot_state.json'


class StateStore:
    """Load and atomically save bot state as JSON.

    The state is a convenience, not a config: any error is logged and ignored
    so a broken or read-only state file never stops the bot.
    """

    def __init__(self, path: Path = STATE_FILE_PATH) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._path = path

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as err:
            self._log.warning('Ignoring unreadable state "%s": %s', self._path, err)
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        tmp_path = self._path.with_name(f'{self._path.name}.tmp')
        try:
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            tmp_path.replace(self._path)
        except OSError as err:
            self._log.warning('Failed to save state "%s": %s', self._path, err)
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
