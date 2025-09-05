# Disable auto-loading of 3rd-party pytest plugins (e.g. web3.tools.pytest_ethereum)
import os
os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
