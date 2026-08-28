"""Path + dependency bootstrap for the qa test suite.

The repo root carries an ``__init__.py``, which makes pytest treat it as a
package and insert its PARENT on ``sys.path`` -- so bare imports like
``hub_client`` fail. Put the repo root on the path explicitly.

``qa_engine`` pulls in ``webui_client`` -> ``playwright``, a heavyweight browser
dependency that is genuinely required at runtime but not to exercise the pure
scenario-filtering logic. Stub it when it is absent so the unit tests stay fast
and runnable without a browser install.
"""
import sys
import types
from pathlib import Path

QA_ROOT = Path(__file__).resolve().parent.parent
if str(QA_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_ROOT))

try:  # pragma: no cover - exercised only where playwright is installed
    import playwright.async_api  # noqa: F401
except ImportError:
    pw = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    api.async_playwright = lambda *a, **k: None
    api.expect = lambda *a, **k: None
    pw.async_api = api
    sys.modules.setdefault("playwright", pw)
    sys.modules.setdefault("playwright.async_api", api)
