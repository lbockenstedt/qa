import asyncio
from playwright.async_api import async_playwright, expect
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("WebUIClient")

class WebUIClient:
    """
    Automates interactions with the Lab Manager WebUI using Playwright.
    """
    def __init__(self, hub_host: str, hub_port: int = 8000):
        self.url = f"http://{hub_host}:{hub_port}"
        self.browser = None
        self.page = None
        self.context = None

    async def start(self):
        """Starts the browser and creates a new page."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        logger.info("WebUI Browser started.")

    async def login(self, username, password):
        """Performs login to the WebUI."""
        logger.info(f"Attempting login for user: {username}")
        await self.page.goto(f"{self.url}/login")

        # Assuming standard login fields
        await self.page.fill('input[name="username"]', username)
        await self.page.fill('input[name="password"]', password)
        await self.page.click('button[type="submit"]')

        # Wait for navigation to dashboard
        try:
            await self.page.wait_for_url(f"{self.url}/dashboard", timeout=10000)
            logger.info("Login successful.")
            return True
        except Exception as e:
            logger.error(f"Login failed: {e}")
            return False

    async def verify_element(self, selector: str, expected_text: str = None):
        """Checks if an element exists and optionally contains specific text."""
        try:
            element = self.page.locator(selector)
            await expect(element).to_be_visible(timeout=5000)
            if expected_text:
                await expect(element).to_contain_text(expected_text)
            return True
        except Exception as e:
            logger.error(f"Element verification failed for {selector}: {e}")
            return False

    async def navigate_to(self, menu_item: str):
        """Navigates through the Left Navigation menu."""
        # The UI layout has primary navigation on the left
        try:
            await self.page.click(f"text={menu_item}")
            logger.info(f"Navigated to {menu_item}")
            return True
        except Exception as e:
            logger.error(f"Navigation to {menu_item} failed: {e}")
            return False

    async def stop(self):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        await self.playwright.stop()
        logger.info("WebUI Browser stopped.")
