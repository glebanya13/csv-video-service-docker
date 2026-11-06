"""mod_wsgi entry point: Apache imports this file and serves `application`."""

import logging
import sys

# mod_wsgi sends stderr to Apache's error log, which goes to `docker compose logs web`.
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from web_service import create_app  # noqa: E402

application = create_app()
