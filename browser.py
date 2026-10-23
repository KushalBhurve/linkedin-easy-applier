import os
import time
import random
import logging
from pathlib import Path
from typing import Optional
from playwright.sync_api import sync_playwright, BrowserContext, Page
from rich.console import Console

logger = logging.getLogger("linkedin_easy_applier.browser")
console = Console(highlight=False)

DEFAULT_USER_DATA_DIR = Path(__file__).parent / ".browser_profile"

class BrowserManager:
    def __init__(
        self,
        user_data_dir: Path = DEFAULT_USER_DATA_DIR,
        headless: bool = False,
        min_delay: float = 2.0,
        max_delay: float = 5.0
    ):
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def start(self) -> Page:
        """Starts Playwright with persistent context and stealth anti-detection."""
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.playwright = sync_playwright().start()

        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--ignore-certificate-errors",
            "--start-maximized"
        ]

        # Use persistent context to retain login cookies & prevent repeated 2FA challenges
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            channel="chromium",
            args=args,
            viewport=None,  # Allows native window size
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        # Apply stealth scripts to all pages
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

        try:
            from playwright_stealth import stealth_sync
            stealth_sync(self.page)
        except Exception as e:
            logger.debug(f"Stealth sync notice: {e}")

        # Inject webdriver evasion script
        self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        return self.page

    def sleep(self, min_s: Optional[float] = None, max_s: Optional[float] = None):
        """Randomized sleep interval to emulate human reaction times."""
        low = min_s if min_s is not None else self.min_delay
        high = max_s if max_s is not None else self.max_delay
        time.sleep(random.uniform(low, high))

    def human_type(self, locator, text: str):
        """Types text character by character with randomized typing cadence."""
        locator.click()
        # Clear field first
        locator.fill("")
        self.sleep(0.3, 0.6)
        for char in str(text):
            locator.press_sequentially(char, delay=random.randint(35, 95))
            if random.random() < 0.05:
                time.sleep(random.uniform(0.1, 0.25))

    def human_scroll(self, element=None, distance: int = 400):
        """Performs smooth scrolling with small steps."""
        target = element if element else self.page
        step = distance // 4
        for _ in range(4):
            if element:
                element.evaluate(f"(el) => el.scrollBy(0, {step})")
            else:
                self.page.mouse.wheel(0, step)
            time.sleep(random.uniform(0.1, 0.25))

    def is_logged_in(self) -> bool:
        """Verifies if the browser has an active authenticated LinkedIn session."""
        try:
            self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=25000)
            self.sleep(2.0, 3.5)
            
            curr_url = self.page.url.lower()
            if "/feed" in curr_url or "/mynetwork" in curr_url or "/jobs" in curr_url:
                # Confirm presence of navigation bar
                nav = self.page.locator("nav.global-nav, .global-nav__me, #global-nav-search")
                if nav.count() > 0:
                    return True

            # If redirected to login/checkpoint/signup
            if any(term in curr_url for term in ["/login", "/checkpoint", "/signup", "/uas/login"]):
                return False

            # Check for guest feed or login button
            guest_btn = self.page.locator("a[href*='/login'], .nav__button-secondary")
            if guest_btn.count() > 0:
                return False

            return True
        except Exception as e:
            logger.warning(f"Error checking login state: {e}")
            return False

    def ensure_login(self) -> bool:
        """
        Ensures the user is logged into LinkedIn. If not, prompts for manual login
        so the user can solve any 2FA/CAPTCHA safely. Session is preserved in .browser_profile.
        """
        console.print("[cyan]Checking LinkedIn authentication session...[/cyan]")
        if self.is_logged_in():
            console.print("[bold green]✓ Authenticated session active in browser profile![/bold green]\n")
            return True

        console.print("\n[bold yellow]⚠️ No active LinkedIn session found in browser profile.[/bold yellow]")
        console.print("[yellow]Opening LinkedIn login page in the visible browser window...[/yellow]")
        console.print("[dim]You only need to do this ONCE. Your session cookies will be saved for all future runs.[/dim]\n")

        self.page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        
        console.print("[bold cyan]👉 Please log into your LinkedIn account in the open browser window.[/bold cyan]")
        console.print("[bold cyan]   (Complete any SMS/App 2FA verification or CAPTCHA if prompted).[/bold cyan]\n")
        
        try:
            input("Press [ENTER] here in the terminal once you have successfully logged in...")
        except EOFError:
            pass

        # Give page a moment to load home feed
        self.sleep(3.0, 5.0)

        if self.is_logged_in():
            console.print("[bold green]✓ Login successful! Session profile saved.[/bold green]\n")
            return True
        else:
            console.print("[bold red]✗ Could not confirm login. Please verify the browser is logged in and try again.[/bold red]")
            return False

    def close(self):
        """Safely closes context and playwright instances."""
        try:
            if self.context:
                self.context.close()
            if self.playwright:
                self.playwright.stop()
        except Exception as e:
            logger.debug(f"Error while closing browser: {e}")
