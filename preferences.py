"""Remember transfer folders locally; SSH keys and passwords are not stored."""

import json
import os
from pathlib import Path
import tempfile

from diagnostics import LOGGER
from headset import HeadsetError, validate_address
from transfers import validate_destination


DEFAULT_DESTINATION = "~/Downloads"


class DestinationPreferences:
    def __init__(self, path: Path | None = None):
        if path is None:
            local_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            path = local_data / "Frame Connect" / "settings.json"
        self.path = Path(path)
        self._data = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("destinations", {}), dict):
                raise ValueError("Invalid destination preferences")
            self._data = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            LOGGER.warning("Could not read saved transfer folders: %s", error)

    @staticmethod
    def _key(host, user):
        return f"{user.strip()}@{host.strip().lower()}"

    def destination(self, host, user):
        path = self._data.get("destinations", {}).get(self._key(host, user), DEFAULT_DESTINATION)
        try:
            return validate_destination(path)
        except HeadsetError:
            return DEFAULT_DESTINATION

    def remember(self, host, user, destination):
        validate_address(host.strip(), user.strip())
        destination = validate_destination(destination)
        destinations = dict(self._data.get("destinations", {}))
        key = self._key(host, user)
        if destination == DEFAULT_DESTINATION:
            destinations.pop(key, None)
        else:
            destinations[key] = destination
        data = dict(self._data, destinations=destinations)
        if destinations == self._data.get("destinations", {}):
            return True
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary.replace(self.path)
            self._data = data
            return True
        except OSError as error:
            LOGGER.warning("Could not save the transfer folder for next launch: %s", error)
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
