"""Request normal shutdown through Steam, only when explicitly run over SSH."""

import json

from steam_api import query_steam


POWER_OFF_QUERY = """(async () => {
    const system = globalThis.SteamClient?.System;
    if (typeof system?.ShutdownPC !== 'function') {
        return {requested: false, error: 'This Steam version does not expose remote power off.'};
    }
    const result = await system.ShutdownPC();
    if (result === false) {
        return {requested: false, error: 'Steam declined the power-off request.'};
    }
    return {requested: true};
})()"""


def request_power_off() -> dict:
    result = query_steam(POWER_OFF_QUERY)
    if isinstance(result, dict) and type(result.get("requested")) is bool:
        return result
    return {"requested": False, "error": (
        "Steam did not confirm the power-off request. Check the headset before trying again. "
        "Its Steam control interface may be unavailable."
    )}


if __name__ == "__main__":
    print(json.dumps(request_power_off()))
