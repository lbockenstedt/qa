from lm.core.src.base_spoke import BaseSpoke
from .test_engine import TestEngine
import logging

logger = logging.getLogger("QASpoke")

class QASpoke(BaseSpoke):
    """
    QA Auditor Spoke implementation.
    Wraps the TestEngine to allow triggering audits from the Hub.
    """
    def __init__(self, spoke_id: str, config: dict):
        super().__init__(spoke_id, config)
        self.last_results = []
        self.engine = None
        # The engine is initialized lazily or via a separate method
        # because it needs the active session secret from the control plane.

    async def set_engine(self, engine):
        """Inject the TestEngine instance."""
        self.engine = engine
        logger.info("TestEngine injected into QASpoke")

    async def handle_command(self, command_type: str, data: dict):
        if command_type == "QA_RUN_TESTS":
            if not self.engine:
                return {"status": "ERROR", "message": "TestEngine not initialized"}

            logger.info("Triggering full QA audit suite...")
            results = await self.engine.run_all()
            self.last_results = results

            # Summarize results
            passed = len([r for r in results if r['status'] == 'PASS'])
            total = len(results)
            return {
                "status": "SUCCESS",
                "summary": f"{passed}/{total} tests passed",
                "results": results
            }

        if command_type == "QA_GET_LAST_RESULTS":
            if not self.last_results:
                return {"status": "SUCCESS", "results": [], "message": "No tests have been run yet."}
            return {"status": "SUCCESS", "results": self.last_results}

        return None

    async def get_status(self):
        return {
            "spoke_id": self.spoke_id,
            "status": "ONLINE",
            "last_run_summary": f"{len([r for r in self.last_results if r['status'] == 'PASS'])}/{len(self.last_results)}" if self.last_results else "No runs yet"
        }
