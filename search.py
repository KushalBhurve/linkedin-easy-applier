import time
import urllib.parse
import logging
import hashlib
import re
from typing import List, Optional, Set, Tuple
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
        
        # In-session deduplication across different search queries, pages, and locations
        self.session_seen_job_ids: Set[str] = set()
        self.session_seen_jobs: Set[Tuple[str, str]] = set()

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
                
                # Navigate to page 1
                search_url = self._build_search_url(role, loc, start=0)
                try:
                    self.page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                    self.browser_mgr.sleep(3.0, 5.0)
                except Exception as e:
                    console.print(f"[red]Failed to load search page for '{role}' in '{loc_label}': {e}[/red]")
                    continue

                # Process job cards on the page (with multi-page navigation)
                apps_in_batch = self._process_search_results(role, loc, max_apps - processed_this_session)
                processed_this_session += apps_in_batch

        console.print(f"\n[bold green]🏁 Finished search run! Total processed this session: {processed_this_session}[/bold green]")

    def _build_search_url(self, role: str, location: str, start: int = 0) -> str:
        """Constructs a targeted LinkedIn job search URL with Easy Apply filter enabled and pagination start offset."""
        base = "https://www.linkedin.com/jobs/search/?"
        
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

        # Pagination start offset (LinkedIn uses 25 items per page: page 1 = 0, page 2 = 25, page 3 = 50)
        if start > 0:
            params["start"] = str(start)

        return base + urllib.parse.urlencode(params)

    def _process_search_results(self, role: str, location: str, remaining_quota: int) -> int:
        """Iterates through job cards on the left panel, filters suitability, and applies across multiple pages."""
        processed_count = 0
        max_pages = 4  # Up to 100 jobs per role/location query

        for current_page in range(1, max_pages + 1):
            if processed_count >= remaining_quota:
                break

            console.print(f"\n[bold cyan]📄 Page {current_page} of search results for '{role}'...[/bold cyan]")

            if current_page > 1:
                nav_success = self._go_to_page(role, location, current_page)
                if not nav_success:
                    console.print(f"[yellow]Could not navigate to Page {current_page} or reached the end of results.[/yellow]")
                    break

            # Scroll down the left job listing panel to trigger lazy loading of all 25 job cards
            self._scroll_job_list()

            # Find job cards
            job_cards = self._collect_job_cards()
            console.print(f"[dim]Found {len(job_cards)} Easy Apply jobs on page {current_page}[/dim]")

            if not job_cards:
                console.print("[yellow]No more job cards found on this page.[/yellow]")
                break

            new_jobs_on_page = 0

            for idx, job in enumerate(job_cards):
                if processed_count >= remaining_quota:
                    break

                job_key = (job.title.lower().strip(), job.company.lower().strip())

                # Session-level deduplication: Skip if seen earlier in this run
                if job.job_id in self.session_seen_job_ids or (job.company != "Unknown" and job_key in self.session_seen_jobs):
                    logger.debug(f"Skipping {job.title} at {job.company} (already seen in this session)")
                    continue

                self.session_seen_job_ids.add(job.job_id)
                if job.company != "Unknown":
                    self.session_seen_jobs.add(job_key)
                new_jobs_on_page += 1

                console.print(f"\n[bold]📋 Job:[/bold] [cyan]{job.title}[/cyan] at [green]{job.company}[/green] ({job.location})")

                # Step 1: Quick Suitability Check (Title & Blacklist & DB History)
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

                # Step 2: Click job card to view detail panel on the right and resolve exact job ID
                self._click_job_card(job, idx)
                self.browser_mgr.sleep(1.5, 3.0)

                # Ensure exact numeric ID is also marked seen in session
                self.session_seen_job_ids.add(job.job_id)

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

            # If an entire page had 0 new jobs, we've likely seen them all or hit pagination boundary
            if new_jobs_on_page == 0 and len(job_cards) > 0:
                console.print(f"[dim]All {len(job_cards)} jobs on page {current_page} were already processed earlier in this session.[/dim]")

        return processed_count

    def _go_to_page(self, role: str, location: str, page_num: int) -> bool:
        """
        Navigates to the specified page (1-indexed) by clicking the pagination button
        or using direct LinkedIn URL offset navigation (start=(page_num-1)*25).
        """
        # Step 1: Scroll to the bottom of the list to reveal the pagination controls
        try:
            container = self.page.locator(".jobs-search-results-list, .scaffold-layout__list").first
            if container.count() > 0 and container.is_visible():
                container.evaluate("el => el.scrollTop = el.scrollHeight")
            else:
                self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self.browser_mgr.sleep(1.0, 2.0)
        except Exception:
            pass

        # Step 2: Attempt clicking page button inside LinkedIn's pagination controls
        page_btn_selectors = [
            f"li[data-test-pagination-page-btn='{page_num}'] button",
            f".artdeco-pagination__pages button[aria-label='Page {page_num}']",
            f".jobs-search-results-list__pagination button[aria-label='Page {page_num}']",
            f"button.artdeco-pagination__indicator--number[aria-label='Page {page_num}']",
            f"button[aria-label='Page {page_num}']",
            f".artdeco-pagination__button--next:not([disabled])",
            f".jobs-search-results-list__pagination button[aria-label='Next']:not([disabled])"
        ]

        clicked = False
        for sel in page_btn_selectors:
            btn = self.page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                try:
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    clicked = True
                    self.browser_mgr.sleep(3.0, 5.0)
                    break
                except Exception as e:
                    logger.debug(f"Could not click pagination button '{sel}': {e}")

        # If button click loaded new unseen jobs, return success
        if clicked:
            self._scroll_job_list()
            cards = self._collect_job_cards()
            unseen = [c for c in cards if c.job_id not in self.session_seen_job_ids]
            if unseen:
                return True

        # Step 3: Reliable URL-based offset pagination fallback (start=25, 50, etc.)
        start_offset = (page_num - 1) * 25
        page_url = self._build_search_url(role, location, start=start_offset)
        console.print(f"[cyan]Navigating to Page {page_num} via URL (start={start_offset})...[/cyan]")
        try:
            self.page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            self.browser_mgr.sleep(3.0, 5.0)
            self._scroll_job_list()
            cards = self._collect_job_cards()
            if cards:
                return True
            else:
                console.print(f"[yellow]No job cards returned on Page {page_num}. End of results reached.[/yellow]")
                return False
        except Exception as e:
            console.print(f"[red]Failed to navigate to Page {page_num}: {e}[/red]")
            return False

    def _collect_job_cards(self) -> List[JobCard]:
        """Finds all visible job card elements in the left panel and extracts metadata with accurate job IDs."""
        cards = []

        # Strategy 1: Modern 2026 search layout (div[role="button"] cards in left rail)
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

                    # Extract real LinkedIn numeric Job ID if available
                    job_id = (
                        item.get_attribute("data-job-id") or
                        item.get_attribute("data-occludable-job-id") or ""
                    )

                    if not job_id:
                        try:
                            links = item.locator("a[href*='/jobs/view/'], a[href*='currentJobId=']").all()
                            for link in links:
                                href = link.get_attribute("href") or ""
                                m = re.search(r"/(?:jobs/view/|currentJobId=)(\d+)", href)
                                if m:
                                    job_id = m.group(1)
                                    break
                        except Exception:
                            pass

                    if not job_id:
                        try:
                            urn = item.get_attribute("data-entity-urn") or item.get_attribute("data-chameleon-result-urn") or ""
                            if not urn:
                                urn_elem = item.locator("[data-entity-urn]").first
                                if urn_elem.count() > 0:
                                    urn = urn_elem.get_attribute("data-entity-urn") or ""
                            m = re.search(r"jobPosting:(\d+)", urn)
                            if m:
                                job_id = m.group(1)
                        except Exception:
                            pass

                    # Stable fallback hash based purely on (title, company) WITHOUT index {i}
                    if not job_id:
                        norm_key = f"{title.lower().strip()}_{company.lower().strip()}"
                        job_id = f"card_{hashlib.md5(norm_key.encode()).hexdigest()[:12]}"

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
                        job_id = m.group(1) if m else None

                    if not job_id:
                        norm_key = f"{title.lower().strip()}_{company.lower().strip()}"
                        job_id = f"card_{hashlib.md5(norm_key.encode()).hexdigest()[:12]}"

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

        # Deduplicate within the page
        seen = set()
        deduped = []
        for c in cards:
            if c.job_id not in seen:
                seen.add(c.job_id)
                deduped.append(c)
        return deduped

    def _click_job_card(self, job: JobCard, index: int):
        """Clicks the job card item to display details in right pane and resolves currentJobId."""
        # Strategy 1: Modern layout card click by index
        modern_cards = self.page.locator("div[role='button']").filter(has_text="Easy Apply")
        if modern_cards.count() > index:
            try:
                target = modern_cards.nth(index)
                target.scroll_into_view_if_needed()
                target.click()
                self.browser_mgr.sleep(1.5, 2.5)

                # Extract exact numeric job ID from URL
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
                self.browser_mgr.sleep(1.5, 2.5)
                m = re.search(r"currentJobId=(\d+)", self.page.url)
                if m:
                    job.job_id = m.group(1)
                    job.job_url = f"https://www.linkedin.com/jobs/view/{job.job_id}/"
            except Exception:
                pass

    def _scroll_job_list(self):
        """Smoothly scrolls the jobs list container down to trigger lazy rendering of all 25 cards."""
        container = self.page.locator(".jobs-search-results-list, .scaffold-layout__list, ul.jobs-search-results__list").first
        is_container_valid = container.count() > 0 and container.is_visible()

        # Step-scroll through the height of the left rail (~3600px to reach all 25 jobs)
        for _ in range(7):
            if is_container_valid:
                self.browser_mgr.human_scroll(container, distance=450)
            else:
                self.page.mouse.wheel(0, 450)
            time.sleep(0.35)

        # Allow lazy DOM elements and pagination bar to finish rendering
        time.sleep(0.8)

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

                // Modern search: Find heading strictly containing 'About the job'
                const headings = Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6, strong'));
                for (const h of headings) {
                    const txt = (h.innerText || '').trim().toLowerCase();
                    if (txt === 'about the job') {
                        let cur = h.parentElement;
                        while (cur && cur.tagName !== 'BODY') {
                            const len = (cur.innerText || '').trim().length;
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
