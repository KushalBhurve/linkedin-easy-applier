import time
import urllib.parse
import logging
from typing import List, Optional
from playwright.sync_api import Page
from rich.console import Console

from models import AppConfig, JobCard, ApplicationResult
from browser import BrowserManager
from filter import JobSuitabilityFilter
from form_filler import EasyApplyFormFiller
from tracker import ApplicationTracker

logger = logging.getLogger("linkedin_easy_applier.search")
console = Console(highlight=False)

DATE_FILTER_MAP = {
    "past_24h": "r86400",
    "past_week": "r604800",
    "past_month": "r2592000",
    "any_time": ""
}

class LinkedInJobSearcher:
    def __init__(
        self,
        page: Page,
        browser_mgr: BrowserManager,
        config: AppConfig,
        tracker: ApplicationTracker
    ):
        self.page = page
        self.browser_mgr = browser_mgr
        self.config = config
        self.tracker = tracker
        self.suitability_filter = JobSuitabilityFilter(
            config.suitability_filter,
            tracker,
            candidate=config.candidate,
            ai_config=config.ai_fallback
        )
        self.form_filler = EasyApplyFormFiller(page, browser_mgr, config)

    def run(self, roles: Optional[List[str]] = None, location: Optional[str] = None):
        """Runs the search and application loop across specified roles and locations."""
        target_roles = roles if roles else self.config.search.roles
        if location:
            target_locations = [location]
        elif getattr(self.config.search, "locations", None):
            target_locations = self.config.search.locations
        else:
            target_locations = [self.config.search.location]

        max_apps = self.config.execution.max_applications_per_run

        console.print(f"[bold cyan]🔍 Target Roles:[/bold cyan] {', '.join(target_roles)}")
        console.print(f"[bold cyan]📍 Target Locations:[/bold cyan] {', '.join(target_locations)}")
        console.print(f"[bold cyan]🛡️  Dry Run Mode:[/bold cyan] {'ENABLED (Test mode - will not submit)' if self.config.execution.dry_run else 'DISABLED (LIVE SUBMISSION)'}")
        console.print(f"[bold cyan]🎯 Session Application Cap:[/bold cyan] {max_apps}\n")

        processed_this_session = 0

        for role in target_roles:
            if processed_this_session >= max_apps:
                console.print(f"[bold green]✓ Reached session limit of {max_apps} applications. Stopping gracefully.[/bold green]")
                break

            for loc in target_locations:
                if processed_this_session >= max_apps:
                    break

                loc_label = "Remote (India)" if loc.lower() == "remote" else loc
                console.print(f"\n[bold yellow]════════ Searching for: '{role}' in '{loc_label}' ════════[/bold yellow]")
                search_url = self._build_search_url(role, loc)
                
                try:
                    self.page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                    self.browser_mgr.sleep(3.0, 5.0)
                except Exception as e:
                    console.print(f"[red]Failed to load search page for '{role}' in '{loc_label}': {e}[/red]")
                    continue

                # Process job cards on the page
                apps_in_batch = self._process_search_results(role, max_apps - processed_this_session)
                processed_this_session += apps_in_batch

        console.print(f"\n[bold green]🏁 Finished search run! Total processed this session: {processed_this_session}[/bold green]")

    def _build_search_url(self, role: str, location: str) -> str:
        """Constructs a targeted LinkedIn job search URL with Easy Apply filter enabled."""
        base = "https://www.linkedin.com/jobs/search-results/?"
        
        is_remote_search = location.lower() == "remote"
        # When searching for remote jobs, target location="India" with f_WT=2 (Remote)
        search_loc = "India" if is_remote_search else location

        params = {
            "keywords": role,
            "location": search_loc,
            "f_AL": "true"
        }

        # Workplace type filter
        if is_remote_search:
            params["f_WT"] = "2"  # 2 = Remote
        elif getattr(self.config.search, "workplace_types", None):
            params["f_WT"] = ",".join(str(wt) for wt in self.config.search.workplace_types)

        # Date posted filter
        date_key = self.config.search.date_posted
        tpr_val = DATE_FILTER_MAP.get(date_key, "")
        if tpr_val:
            params["f_TPR"] = tpr_val

        # Experience levels filter
        if self.config.search.experience_levels:
            exp_str = ",".join(str(lvl) for lvl in self.config.search.experience_levels)
            params["f_E"] = exp_str

        return base + urllib.parse.urlencode(params)

    def _process_search_results(self, role: str, remaining_quota: int) -> int:
        """Iterates through job cards on the left panel, filters suitability, and applies."""
        processed_count = 0
        max_pages = 3

        for current_page in range(1, max_pages + 1):
            if processed_count >= remaining_quota:
                break

            # Scroll down the left job listing panel to trigger lazy loading of all job cards
            self._scroll_job_list()

            # Find job cards
            job_cards = self._collect_job_cards()
            console.print(f"[dim]Found {len(job_cards)} Easy Apply jobs on page {current_page}[/dim]")

            if not job_cards:
                console.print("[yellow]No more job cards found on this page.[/yellow]")
                break

            for idx, job in enumerate(job_cards):
                if processed_count >= remaining_quota:
                    break

                console.print(f"\n[bold]📋 Job:[/bold] [cyan]{job.title}[/cyan] at [green]{job.company}[/green] ({job.location})")

                # Step 1: Quick Suitability Check (Title & Blacklist & History)
                is_suitable, card_reason = self.suitability_filter.check_job_card(job)
                if not is_suitable:
                    console.print(f"  [dim]⏭️  Skipping: {card_reason}[/dim]")
                    self.tracker.record_application(ApplicationResult(
                        job_id=job.job_id,
                        title=job.title,
                        company=job.company,
                        location=job.location,
                        job_url=job.job_url,
                        status="skipped_filter",
                        reason=card_reason,
                        applied_at=time.strftime('%Y-%m-%dT%H:%M:%S')
                    ))
                    continue

                # Step 2: Click job card to view detail panel on the right
                self._click_job_card(job, idx)
                self.browser_mgr.sleep(1.5, 3.0)

                # Step 3: Deep check of job description text
                desc_text = self._get_job_description_text()
                desc_suitable, desc_reason = self.suitability_filter.check_job_description(job.title, desc_text)
                if not desc_suitable:
                    console.print(f"  [dim]⏭️  Skipping: {desc_reason}[/dim]")
                    self.tracker.record_application(ApplicationResult(
                        job_id=job.job_id,
                        title=job.title,
                        company=job.company,
                        location=job.location,
                        job_url=job.job_url,
                        status="skipped_experience",
                        reason=desc_reason,
                        applied_at=time.strftime('%Y-%m-%dT%H:%M:%S')
                    ))
                    continue

                # Step 4: Open Easy Apply and process form
                console.print(f"  [bold blue]📝 Starting Easy Apply process...[/bold blue]")
                result = self.form_filler.apply(job)
                self.tracker.record_application(result)

                if result.status in ["applied", "dry_run_success"]:
                    processed_count += 1
                    status_text = "[bold green]SUBMITTED[/bold green]" if result.status == "applied" else "[bold cyan]DRY-RUN PASSED[/bold cyan]"
                    console.print(f"  ✓ {status_text}: {result.reason}")
                    
                    # Humanized cooldown between job submissions
                    delay = self.config.execution.delay_between_jobs_sec
                    console.print(f"  [dim]Pacing delay: waiting {delay:.1f}s before next job...[/dim]")
                    self.browser_mgr.sleep(delay * 0.8, delay * 1.2)
                else:
                    console.print(f"  [yellow]Notice:[/yellow] {result.status} - {result.reason}")
                    self.browser_mgr.sleep(2.0, 4.0)

            # Check next page button if more quota needed
            if processed_count < remaining_quota:
                has_next = self._click_next_page(current_page + 1)
                if not has_next:
                    break
                self.browser_mgr.sleep(3.0, 5.0)

        return processed_count

    def _collect_job_cards(self) -> List[JobCard]:
        """Finds all visible job card elements in the left panel and extracts metadata."""
        import hashlib
        import re

        cards = []

        # Strategy 1: Modern 2026 AI-search layout (div[role="button"] cards in left rail)
        modern_locators = self.page.locator("div[role='button']").filter(has_text="Easy Apply")
        modern_count = modern_locators.count()

        if modern_count > 0:
            for i in range(modern_count):
                try:
                    item = modern_locators.nth(i)
                    full_text = item.inner_text().strip()
                    lines = [s.strip() for s in full_text.split("\n") if s.strip()]
                    if not lines:
                        continue

                    # First line is title
                    raw_title = lines[0]
                    title = re.sub(r"\(Verified job\)", "", raw_title, flags=re.I).strip()

                    # Company is typically line 2 or 3
                    company = "Unknown"
                    for line in lines[1:]:
                        if line.lower() != title.lower() and not any(term in line.lower() for term in ["actively reviewing", "easy apply", "posted", "applicant", "viewed", "ago", "verified"]):
                            company = line
                            break

                    # Location
                    location = self.config.search.location
                    for line in lines:
                        if any(term in line.lower() for term in ["(on-site)", "(remote)", "(hybrid)", "bengaluru", "bangalore", "india", "hyderabad", "pune", "mumbai"]):
                            location = line
                            break

                    # Deterministic hash ID
                    job_id = hashlib.md5(f"{title}_{company}_{i}".encode()).hexdigest()[:10]

                    cards.append(JobCard(
                        job_id=job_id,
                        title=title,
                        company=company,
                        location=location,
                        job_url=self.page.url,
                        is_easy_apply=True
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing modern job card {i}: {e}")

        # Strategy 2: Classic layout fallback
        if not cards:
            locators = self.page.locator(".jobs-search-results__list-item, [data-occludable-job-id], .job-card-container")
            count = locators.count()

            for i in range(count):
                item = locators.nth(i)
                try:
                    job_id = item.get_attribute("data-occludable-job-id") or item.get_attribute("data-job-id")
                    title_elem = item.locator("a.job-card-list__title, a.job-card-container__link").first
                    if title_elem.count() == 0:
                        continue
                    title = title_elem.inner_text().strip()
                    href = title_elem.get_attribute("href") or ""
                    job_url = "https://www.linkedin.com" + href if href.startswith("/") else href

                    if not job_id:
                        m = re.search(r"/view/(\d+)", job_url)
                        job_id = m.group(1) if m else str(int(time.time() * 1000) + i)

                    comp_elem = item.locator(".job-card-container__primary-description, .artdeco-entity-lockup__subtitle").first
                    company = comp_elem.inner_text().strip() if comp_elem.count() > 0 else "Unknown"

                    loc_elem = item.locator(".job-card-container__metadata-item").first
                    loc_text = loc_elem.inner_text().strip() if loc_elem.count() > 0 else ""

                    cards.append(JobCard(
                        job_id=job_id,
                        title=title,
                        company=company,
                        location=loc_text,
                        job_url=job_url,
                        is_easy_apply=True
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing classic job card {i}: {e}")

        # Deduplicate
        seen = set()
        deduped = []
        for c in cards:
            if c.job_id not in seen:
                seen.add(c.job_id)
                deduped.append(c)
        return deduped

    def _click_job_card(self, job: JobCard, index: int):
        """Clicks the job card item to display details in right pane and resolves currentJobId."""
        import re

        # Strategy 1: Modern layout card click by index
        modern_cards = self.page.locator("div[role='button']").filter(has_text="Easy Apply")
        if modern_cards.count() > index:
            try:
                target = modern_cards.nth(index)
                target.scroll_into_view_if_needed()
                target.click()
                self.browser_mgr.sleep(1.5, 2.5)

                # Extract exact job ID from URL
                m = re.search(r"currentJobId=(\d+)", self.page.url)
                if m:
                    job.job_id = m.group(1)
                    job.job_url = f"https://www.linkedin.com/jobs/view/{job.job_id}/"
                return
            except Exception as e:
                logger.debug(f"Notice clicking modern card {index}: {e}")

        # Strategy 2: Classic selector
        card = self.page.locator(f"[data-occludable-job-id='{job.job_id}'], [data-job-id='{job.job_id}']")
        if card.count() > 0:
            try:
                card.first.scroll_into_view_if_needed()
                card.first.click()
            except Exception:
                pass

    def _scroll_job_list(self):
        """Smoothly scrolls the jobs list container down to trigger lazy rendering."""
        # Check classic container
        container = self.page.locator(".jobs-search-results-list, .scaffold-layout__list").first
        if container.count() > 0 and container.is_visible():
            for _ in range(3):
                self.browser_mgr.human_scroll(container, distance=400)
                time.sleep(0.3)
        else:
            # Modern layout: scroll the left pane
            cards = self.page.locator("div[role='button']").filter(has_text="Easy Apply")
            if cards.count() > 0:
                try:
                    last_card = cards.last
                    last_card.scroll_into_view_if_needed()
                except Exception:
                    pass

    def _get_job_description_text(self) -> str:
        """Extracts text content from the right pane's job description area."""
        # 1. Expand truncated description if 'Show more' is visible
        try:
            more_btn = self.page.locator("button:has-text('Show more'), button:has-text('see more'), button[aria-label*='Show more']").first
            if more_btn.count() > 0 and more_btn.is_visible():
                more_btn.click()
                self.browser_mgr.sleep(0.5, 1.0)
        except Exception:
            pass

        # 2. Extract full JD content
        try:
            desc = self.page.evaluate("""() => {
                // Classic selectors
                const classic = document.querySelector('#job-details, .jobs-description__content, .jobs-box__html-content, .jobs-description');
                if (classic && (classic.innerText || '').trim().length > 50) {
                    return classic.innerText.trim();
                }

                // Modern 2026 search: Find heading strictly containing 'About the job'
                const headings = Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6, strong'));
                for (const h of headings) {
                    const txt = (h.innerText || '').trim().toLowerCase();
                    if (txt === 'about the job') {
                        let cur = h.parentElement;
                        while (cur && cur.tagName !== 'BODY') {
                            const len = (cur.innerText || '').trim().length;
                            // The actual JD section is typically between 150 and 6000 chars
                            if (len > 100 && len < 6000) {
                                return cur.innerText.trim();
                            }
                            cur = cur.parentElement;
                        }
                    }
                }

                // Fallback: Article or main
                const article = document.querySelector('article, main');
                if (article && (article.innerText || '').trim().length > 100) {
                    return article.innerText.trim();
                }

                return '';
            }""")
            if desc:
                return desc
        except Exception as e:
            logger.debug(f"Error extracting JD: {e}")

        return ""

    def _click_next_page(self, page_num: int) -> bool:
        """Finds and clicks the pagination button for the next page."""
        btn = self.page.locator(f"button[aria-label='Page {page_num}'], button[aria-label='Next']")
        if btn.count() > 0 and btn.first.is_visible():
            try:
                btn.first.click()
                return True
            except Exception:
                return False
        return False
