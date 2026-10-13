"""Start the Frame Connect desktop app."""

import platform

from diagnostics import LOGGER, configure_logging


def main():
    configure_logging()
    LOGGER.info("Starting Frame Connect; Python %s; %s", platform.python_version(), platform.platform())
    # Import after logging starts so startup/import failures also leave a trace.
    from app import APP_VERSION, main as run_app

    LOGGER.info("Frame Connect version %s", APP_VERSION)
    run_app()


if __name__ == "__main__":
    main()
