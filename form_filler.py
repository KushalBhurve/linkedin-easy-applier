import os
import re
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple
from playwright.sync_api import Page, Locator
from rich.console import Console

from models import AppConfig, JobCard, ApplicationResult
from llm_helper import answer_screening_question
from browser import BrowserManager

logger = logging.getLogger("linkedin_easy_applier.form_filler")
console = Console(highlight=False)

RESULTS_DIR = Path(__file__).parent / "results"
DRY_RUNS_DIR = RESULTS_DIR / "dry_runs"
ERRORS_DIR = RESULTS_DIR / "errors"

class EasyApplyFormFiller:
    def __init__(self, page: Page, browser_mgr: BrowserManager, config: AppConfig):
        self.page = page
        self.browser_mgr = browser_mgr
        self.config = config
        self.cand = config.candidate
        self.screening_cfg = config.screening_answers
        self.ai_cfg = config.ai_fallback
        DRY_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        ERRORS_DIR.mkdir(parents=True, exist_ok=True)

    def apply(self, job: JobCard) -> ApplicationResult:
        """
        Main entry point for filling the LinkedIn Easy Apply modal for a given job.
        Navigates multi-step screens, handles custom questions, and executes submission or dry run.
        """
        timestamp = datetime.now().isoformat()
        screening_log: Dict[str, Any] = {}

        # 1. Locate and click the 'Easy Apply' button
        apply_btn = self._find_easy_apply_button()
        if not apply_btn:
            return ApplicationResult(
                job_id=job.job_id,
                title=job.title,
                company=job.company,
                location=job.location,
                job_url=job.job_url,
                status="skipped",
                reason="Easy Apply button not found or already applied",
                applied_at=timestamp
            )

        try:
            apply_btn.click()
            self.browser_mgr.sleep(1.5, 3.0)
        except Exception as e:
            return ApplicationResult(
                job_id=job.job_id,
                title=job.title,
                company=job.company,
                location=job.location,
                job_url=job.job_url,
                status="failed_form",
                reason=f"Failed to click Easy Apply button: {e}",
                applied_at=timestamp
            )

        # 2. Check if modal is present by looking for active action buttons or dialog
        next_or_submit = self.page.locator("button:has-text('Next'), button:has-text('Submit application'), button:has-text('Review')")
        try:
            next_or_submit.first.wait_for(state="visible", timeout=4000)
        except Exception:
            return ApplicationResult(
                job_id=job.job_id,
                title=job.title,
                company=job.company,
                location=job.location,
                job_url=job.job_url,
                status="failed_form",
                reason="Easy Apply modal dialog did not appear",
                applied_at=timestamp
            )

        modal_candidates = self.page.locator("div[role='dialog'], [aria-modal='true'], div.jobs-easy-apply-modal, div[data-test-modal]")
        modal = modal_candidates.first if modal_candidates.count() > 0 and modal_candidates.first.is_visible() else self.page

        # 3. Iterate through modal steps (up to 12 steps maximum)
        max_steps = 12
        for step_idx in range(1, max_steps + 1):
            self.browser_mgr.sleep(1.0, 2.0)
            
            # Fill all fields currently present in this step
            step_log = self._fill_current_step(modal)
            screening_log.update(step_log)

            # Check next action button on the active page
            submit_btn = self.page.locator("button[aria-label='Submit application'], button:has-text('Submit application'), button:has-text('Submit')")
            review_btn = self.page.locator("button[aria-label='Review your application'], button:has-text('Review')")
            next_btn = self.page.locator("button[aria-label='Continue to next step'], button:has-text('Next')")

            # If on review screen or submit button exists, ensure it is scrolled into view
            if submit_btn.count() > 0 or self.page.locator("text='Review your application'").count() > 0:
                try:
                    self.page.evaluate("""() => {
                        const dialog = document.querySelector("div[role='dialog'], [aria-modal='true']");
                        if (dialog) dialog.scrollTop = dialog.scrollHeight;
                        window.scrollTo(0, document.body.scrollHeight);
                    }""")
                    self.browser_mgr.sleep(0.5, 1.0)
                    submit_btn = self.page.locator("button[aria-label='Submit application'], button:has-text('Submit application'), button:has-text('Submit')")
                except Exception:
                    pass

            # Check if this is the final submission step
            if submit_btn.count() > 0 and submit_btn.first.is_visible():
                screenshot_path = None
                if self.config.execution.save_screenshots:
                    s_name = f"{job.job_id}_{int(time.time())}.png"
                    s_file = DRY_RUNS_DIR / s_name
                    try:
                        self.page.screenshot(path=str(s_file))
                        screenshot_path = str(s_file)
                    except Exception as e:
                        logger.debug(f"Failed to take screenshot: {e}")

                if self.config.execution.dry_run:
                    console.print(f"  [bold cyan]🔍 [DRY RUN][/bold cyan] Reached final review/submit step. Form fully validated!")
                    if screenshot_path:
                        console.print(f"  [dim]Screenshot saved: {screenshot_path}[/dim]")
                    
                    # Safely discard modal without submitting
                    self._discard_modal(modal)
                    return ApplicationResult(
                        job_id=job.job_id,
                        title=job.title,
                        company=job.company,
                        location=job.location,
                        job_url=job.job_url,
                        status="dry_run_success",
                        reason="Dry-run completed successfully; form was completely valid",
                        screening_log=screening_log,
                        screenshot_path=screenshot_path,
                        applied_at=timestamp
                    )
                else:
                    # LIVE SUBMISSION
                    console.print(f"  [bold green]🚀 Submitting application...[/bold green]")
                    submit_btn.first.click()
                    self.browser_mgr.sleep(3.0, 5.0)

                    # Close any follow-up post-apply modal ("Done" or "Dismiss")
                    done_btn = self.page.locator("button:has-text('Done'), button[aria-label='Dismiss']")
                    if done_btn.count() > 0:
                        try:
                            done_btn.first.click()
                        except Exception:
                            pass

                    return ApplicationResult(
                        job_id=job.job_id,
                        title=job.title,
                        company=job.company,
                        location=job.location,
                        job_url=job.job_url,
                        status="applied",
                        reason="Application submitted successfully on LinkedIn",
                        screening_log=screening_log,
                        screenshot_path=screenshot_path,
                        applied_at=timestamp
                    )

            # If Review button is present, click it
            elif review_btn.count() > 0 and review_btn.first.is_visible():
                console.print(f"  [dim]Step {step_idx}: Reviewing application...[/dim]")
                review_btn.first.click()
                self.browser_mgr.sleep(1.5, 2.5)

                # Check for blocking validation errors on review
                has_err, err_msg = self._detect_and_resolve_errors(modal)
                if has_err:
                    review_btn.first.click()
                    self.browser_mgr.sleep(1.5, 2.5)
                    has_err_still, err_still = self._detect_and_resolve_errors(modal)
                    if has_err_still:
                        err_shot = ERRORS_DIR / f"{job.job_id}_review.png"
                        try:
                            modal.screenshot(path=str(err_shot))
                            console.print(f"  [dim]Error screenshot saved: {err_shot}[/dim]")
                        except Exception:
                            pass
                        console.print(f"  [red]❌ Validation error blocked Review screen:[/red] {err_still}")
                        self._discard_modal(modal)
                        return ApplicationResult(
                            job_id=job.job_id,
                            title=job.title,
                            company=job.company,
                            location=job.location,
                            job_url=job.job_url,
                            status="failed_validation",
                            reason=f"Review validation error: {err_still}",
                            screening_log=screening_log,
                            applied_at=timestamp
                        )

            # If Next button is present, click it
            elif next_btn.count() > 0 and next_btn.first.is_visible():
                console.print(f"  [dim]Step {step_idx}: Completed page, clicking Next...[/dim]")
                next_btn.first.click()
                self.browser_mgr.sleep(1.5, 2.5)

                # Check for blocking validation errors on next
                has_err, err_msg = self._detect_and_resolve_errors(modal)
                if has_err:
                    next_btn.first.click()
                    self.browser_mgr.sleep(1.5, 2.5)
                    has_err_still, err_still = self._detect_and_resolve_errors(modal)
                    if has_err_still:
                        err_shot = ERRORS_DIR / f"{job.job_id}_step_{step_idx}.png"
                        try:
                            modal.screenshot(path=str(err_shot))
                            console.print(f"  [dim]Error screenshot saved: {err_shot}[/dim]")
                        except Exception:
                            pass
                        console.print(f"  [red]❌ Validation error blocked progression at Step {step_idx}:[/red] {err_still}")
                        self._discard_modal(modal)
                        return ApplicationResult(
                            job_id=job.job_id,
                            title=job.title,
                            company=job.company,
                            location=job.location,
                            job_url=job.job_url,
                            status="failed_validation",
                            reason=f"Step {step_idx} validation error: {err_still}",
                            screening_log=screening_log,
                            applied_at=timestamp
                        )
            else:
                # No standard button found
                break

        # If loop exited without submitting or dry run finish
        self._discard_modal(modal)
        return ApplicationResult(
            job_id=job.job_id,
            title=job.title,
            company=job.company,
            location=job.location,
            job_url=job.job_url,
            status="failed_form",
            reason="Could not reach final submission step within maximum steps",
            screening_log=screening_log,
            applied_at=timestamp
        )

    def _find_easy_apply_button(self) -> Optional[Locator]:
        """Locates the visible Easy Apply button on the job page or details pane."""
        selectors = [
            "button.jobs-apply-button",
            "button:has-text('Easy Apply')",
            "div.jobs-apply-button--top-card button",
            "[data-job-id] button:has-text('Easy Apply')"
        ]
        for sel in selectors:
            loc = self.page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                text = loc.first.inner_text().lower()
                if "easy apply" in text or "apply" in text:
                    return loc.first
        return None

    def _fill_current_step(self, modal: Locator) -> Dict[str, Any]:
        """Inspects and answers all form inputs present in the active step."""
        log = {}

        # 1. Contact / Phone Number & Country Code fields
        try:
            target_country = self.cand.country
            target_code = self.cand.phone_country_code.replace("+", "").strip()
            self.page.evaluate("""({ country, code }) => {
                const selects = Array.from(document.querySelectorAll("select[id*='phone' i], select[name*='phone' i], select[aria-label*='country code' i]"));
                for (const sel of selects) {
                    for (let i = 0; i < sel.options.length; i++) {
                        const txt = (sel.options[i].text || '').toLowerCase();
                        if (txt.includes(country.toLowerCase()) && (txt.includes(`+${code}`) || txt.includes(`(${code})`))) {
                            if (sel.selectedIndex !== i) {
                                sel.selectedIndex = i;
                                sel.dispatchEvent(new Event('change', { bubbles: true }));
                                sel.dispatchEvent(new Event('input', { bubbles: true }));
                            }
                            break;
                        }
                    }
                }
            }""", {"country": target_country, "code": target_code})
        except Exception:
            pass

        phone_input = modal.locator("input[id*='phoneNumber'], input[type='tel']")
        if phone_input.count() > 0 and phone_input.first.is_visible():
            val = phone_input.first.input_value().strip()
            if not val:
                self.browser_mgr.human_type(phone_input.first, self.cand.phone)
                log["phone"] = self.cand.phone

        # 2. City / Location Autocomplete field (ensures pre-filled Bangalore is preserved without duplicate typing)
        self._handle_city_input(modal)

        # 3. Resume Upload / Selection
        self._handle_resume(modal)

        # 3. Radio Button groups (handled safely via browser evaluate to eliminate locator timeouts)
        try:
            radio_groups = self.page.evaluate("""() => {
                const radios = Array.from(document.querySelectorAll("input[type='radio']"));
                const groups = {};
                for (let r of radios) {
                    const name = r.name || 'unnamed_' + Math.random();
                    if (!groups[name]) {
                        // Extract question text from closest fieldset or ancestor container
                        let q = '';
                        let p = r.parentElement;
                        while (p && p.tagName !== 'BODY') {
                            const t = (p.innerText || '').trim();
                            if (t && t.length > 5 && !t.startsWith('Yes') && !t.startsWith('No')) {
                                q = t.split('\\n')[0].replace(/[*]/g, '').trim();
                                break;
                            }
                            p = p.parentElement;
                        }
                        groups[name] = { question: q, options: [] };
                    }

                    // Extract label text for this radio option
                    let optText = '';
                    if (r.id) {
                        const lbl = document.querySelector(`label[for="${r.id}"]`);
                        if (lbl) optText = (lbl.innerText || '').trim();
                    }
                    if (!optText) {
                        let cur = r.parentElement;
                        while (cur && cur.tagName !== 'BODY') {
                            const t = (cur.innerText || '').trim();
                            if (t === 'Yes' || t === 'No' || (t.length > 0 && t.length < 35)) {
                                optText = t.split('\\n')[0].trim();
                                break;
                            }
                            cur = cur.parentElement;
                        }
                    }

                    groups[name].options.push({
                        id: r.id || '',
                        value: r.value || '',
                        text: optText || r.value || 'Yes',
                        checked: r.checked
                    });
                }
                return groups;
            }""")

            for r_name, group_data in radio_groups.items():
                question_text = group_data.get("question", "")
                options = [opt["text"] for opt in group_data.get("options", [])]
                chosen = self._resolve_radio_choice(question_text or "available", options or ["Yes", "No"])
                if chosen:
                    # Select option in browser via event dispatch
                    self.page.evaluate("""({ r_name, chosen }) => {
                        const radios = Array.from(document.querySelectorAll(`input[type='radio'][name='${r_name}']`));
                        for (let r of radios) {
                            let optText = '';
                            if (r.id) {
                                const lbl = document.querySelector(`label[for="${r.id}"]`);
                                if (lbl) optText = (lbl.innerText || '').trim();
                            }
                            if (!optText) {
                                let cur = r.parentElement;
                                while (cur && cur.tagName !== 'BODY') {
                                    const t = (cur.innerText || '').trim();
                                    if (t === 'Yes' || t === 'No' || (t.length > 0 && t.length < 35)) {
                                        optText = t.split('\\n')[0].trim();
                                        break;
                                    }
                                    cur = cur.parentElement;
                                }
                            }
                            if ((optText || r.value || '').toLowerCase().includes(chosen.toLowerCase())) {
                                r.checked = true;
                                r.dispatchEvent(new Event('change', { bubbles: true }));
                                r.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                                break;
                            }
                        }
                    }""", {"r_name": r_name, "chosen": chosen})
                    log[question_text or r_name] = chosen
        except Exception as e:
            logger.debug(f"Error handling radio groups: {e}")

        # 4. Dropdowns / Select tags
        selects = self.page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            if not sel.is_visible():
                continue

            try:
                info = sel.evaluate("""el => {
                    let q = el.getAttribute('aria-label') || '';
                    if (!q && el.id) {
                        const lbl = document.querySelector(`label[for="${el.id}"]`);
                        if (lbl) q = (lbl.innerText || '').trim();
                    }
                    if (!q) {
                        let p = el.parentElement;
                        while (p && p.tagName !== 'BODY') {
                            const lbl = p.querySelector('label');
                            if (lbl) { q = (lbl.innerText || '').trim(); break; }
                            p = p.parentElement;
                        }
                    }
                    // If still empty, check name or id
                    if (!q) {
                        q = el.id || el.name || '';
                    }
                    const options = Array.from(el.options).map(o => (o.text || '').trim()).filter(Boolean);
                    return { question: q, options: options };
                }""")
            except Exception:
                continue

            q_text = info.get("question", "")
            opt_texts = info.get("options", [])
            answer = self._resolve_dropdown_choice(q_text, opt_texts)
            if answer:
                try:
                    sel.evaluate("""(el, targetText) => {
                        for (let i = 0; i < el.options.length; i++) {
                            const opt = el.options[i];
                            const txt = (opt.text || '').trim().toLowerCase();
                            if (txt === targetText.toLowerCase() || txt.includes(targetText.toLowerCase())) {
                                el.selectedIndex = i;
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                break;
                            }
                        }
                    }""", answer)
                    log[q_text or f"select_{i}"] = answer
                except Exception as e:
                    logger.debug(f"Error selecting option '{answer}' for '{q_text}': {e}")

        # 5. Text & Number Inputs
        text_inputs = self.page.locator("input[type='text'], input[type='number'], input:not([type]), textarea")
        for i in range(text_inputs.count()):
            inp = text_inputs.nth(i)
            if not inp.is_visible() or inp.get_attribute("type") == "file":
                continue
            
            # Avoid overwriting already populated values (e.g. name, email, phone, prefilled city)
            try:
                curr_val = inp.evaluate("el => (el.value || '').trim()")
                if curr_val:
                    continue
            except Exception:
                pass

            inp_id = inp.get_attribute("id") or ""
            q_text = inp.get_attribute("aria-label") or inp.get_attribute("placeholder") or ""
            if not q_text and inp_id:
                label_loc = self.page.locator(f"label[for='{inp_id}']")
                if label_loc.count() > 0:
                    q_text = label_loc.first.inner_text().strip()

            if not q_text:
                # Try parent container label
                parent = inp.locator("xpath=..")
                q_text = parent.inner_text().strip()

            if not q_text:
                continue

            # CRITICAL: Skip city/location inputs as they are handled exclusively by _handle_city_input
            if any(term in q_text.lower() for term in ["city", "location (city)", "current location"]):
                continue

            is_numeric = (inp.get_attribute("type") == "number") or ("years" in q_text.lower())
            ans = self._resolve_text_input(q_text, is_numeric=is_numeric, inp=inp)
            if ans:
                self.browser_mgr.human_type(inp, str(ans))
                log[q_text] = str(ans)

        # 6. Checkboxes (terms / agreements)
        checkboxes = self.page.locator("input[type='checkbox']")
        for i in range(checkboxes.count()):
            cb = checkboxes.nth(i)
            if not cb.is_visible():
                continue
            # If mandatory terms agreement, ensure checked
            cb_id = cb.get_attribute("id") or ""
            cb_label = cb.get_attribute("aria-label") or ""
            if not cb_label and cb_id:
                label_loc = self.page.locator(f"label[for='{cb_id}']")
                cb_label = label_loc.first.inner_text().lower() if label_loc.count() > 0 else ""
            if any(term in cb_label.lower() for term in ["agree", "terms", "policy", "acknowledge"]):
                if not cb.is_checked():
                    cb.check()
                    log[f"checkbox_{i}"] = "Checked terms"

        return log

    def _handle_resume(self, modal: Locator):
        """Uploads or selects resume if required."""
        file_input = self.page.locator("input[type='file']")
        if file_input.count() > 0:
            resume_path = Path(self.cand.resume_path)
            # Resolve relative to project folder
            if not resume_path.is_absolute():
                resume_path = Path(__file__).parent / resume_path

            if resume_path.exists():
                try:
                    file_input.first.set_input_files(str(resume_path.resolve()))
                    self.browser_mgr.sleep(1.0, 2.0)
                except Exception as e:
                    pass

    def _handle_city_input(self, modal: Locator, force_reselect: bool = False) -> bool:
        """
        Handles LinkedIn's City / Location autocomplete typeahead input.
        If already populated with Bangalore / Bengaluru / Karnataka / India, leaves it completely untouched.
        If empty or corrupted (e.g. concatenated text like 'IndiaBengaluru'), clears properly,
        types Bengaluru, and clicks the autocomplete dropdown option.
        """
        try:
            city_input = modal.locator(
                "input[id*='city' i], input[aria-label*='city' i], input[aria-label*='location' i], input[name*='city' i]"
            ).first
            if city_input.count() == 0 or not city_input.is_visible():
                return False

            val = city_input.evaluate("el => (el.value || '').trim()")
            val_lower = val.lower()

            # If not forcing reselect, check if already properly prefilled
            if not force_reselect:
                if any(term in val_lower for term in ["bangalore", "bengaluru", "karnataka", "india"]):
                    # Ensure it does NOT have obvious concatenation bugs (e.g. 'indiabengaluru')
                    if not ("indiabengaluru" in val_lower or "karnatakabengaluru" in val_lower):
                        logger.debug(f"City field already properly filled with '{val}'. Leaving as is.")
                        return True

            logger.info(f"Setting city field (current value: '{val}')...")
            city_input.click()
            self.browser_mgr.sleep(0.2, 0.4)

            # Clear completely with Ctrl+A -> Backspace and DOM reset
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Backspace")
            city_input.evaluate("""el => {
                el.value = '';
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""")
            self.browser_mgr.sleep(0.3, 0.5)

            # Type 'Bengaluru' to trigger typeahead lookup
            target_city = "Bengaluru"
            for ch in target_city:
                self.page.keyboard.press(ch)
                time.sleep(0.08)

            self.browser_mgr.sleep(1.2, 1.8)

            # Look for the autocomplete popup list item and click it
            clicked = self.page.evaluate("""() => {
                const candidates = Array.from(document.querySelectorAll(
                    "[role='listbox'] [role='option'], .basic-typeahead__selectable-list li, .search-typeahead-v2__hit, div[class*='typeahead'] li"
                ));
                for (const item of candidates) {
                    const txt = (item.innerText || '').toLowerCase();
                    if (txt.includes('bengaluru') || txt.includes('bangalore')) {
                        item.click();
                        return true;
                    }
                }
                return false;
            }""")

            if not clicked:
                # Fallback: Press ArrowDown then Enter to select first typeahead suggestion
                self.page.keyboard.press("ArrowDown")
                self.browser_mgr.sleep(0.3, 0.5)
                self.page.keyboard.press("Enter")

            self.browser_mgr.sleep(0.5, 1.0)
            return True
        except Exception as e:
            logger.debug(f"Error handling city input: {e}")
            return False

    def _detect_and_resolve_errors(self, modal: Locator) -> Tuple[bool, str]:
        """
        Scans the current application step for inline feedback errors, invalid inputs,
        or blocking messages. Attempts self-healing where possible.
        Returns (has_unresolved_error, error_summary).
        """
        try:
            errors = self.page.evaluate("""() => {
                const errElements = Array.from(document.querySelectorAll(
                    ".artdeco-inline-feedback--error, [data-test-form-builder-error], p[class*='feedback--error'], div[class*='feedback--error'], [aria-invalid='true']"
                ));
                const found = [];
                for (const el of errElements) {
                    const txt = (el.innerText || '').trim();
                    if (txt && !found.some(f => f.message === txt)) {
                        let fieldName = '';
                        let p = el.parentElement;
                        while (p && p.tagName !== 'BODY') {
                            const lbl = p.querySelector('label');
                            if (lbl) { fieldName = (lbl.innerText || '').trim(); break; }
                            p = p.parentElement;
                        }
                        found.push({ field: fieldName, message: txt });
                    }
                }
                return found;
            }""")

            if not errors:
                return False, ""

            err_summary = "; ".join(f"{e.get('field', 'Field')}: {e.get('message', '')}" for e in errors)
            logger.warning(f"Detected form validation error(s): {err_summary}")
            console.print(f"  [bold yellow]⚠️ Form validation error detected:[/bold yellow] [red]{err_summary}[/red]")

            # Attempt self-healing based on error type
            for err in errors:
                f_name = err.get("field", "").lower()
                msg = err.get("message", "").lower()

                # 1. City / Location error (e.g. "Please enter a valid city")
                if any(term in f_name for term in ["city", "location"]) or any(term in msg for term in ["city", "location"]):
                    console.print(f"  [cyan]🔧 Auto-resolving city location error...[/cyan]")
                    self._handle_city_input(modal, force_reselect=True)

                # 2. Salary / Compensation validation error (e.g. "Please enter a value greater than or equal to 100000" or entered LPA when full numbers needed)
                elif any(term in f_name for term in ["salary", "ctc", "compensation", "remuneration", "pay"]) or \
                     any(term in msg for term in ["greater than", "less than", "too small", "too large", "at least", "minimum", "maximum", "annual salary"]):
                    salaries = self._get_salary_values()
                    is_expected = "expected" in f_name or "desired" in f_name
                    
                    wants_full = any(term in msg for term in ["greater than", "too small", "at least", "minimum", "more than", "annual salary"])
                    wants_lpa = any(term in msg for term in ["less than", "too large", "at most", "maximum"])
                    
                    if wants_full:
                        target_val = salaries["expected_full"] if is_expected else salaries["current_full"]
                        console.print(f"  [cyan]🔧 Auto-resolving salary error: switching to full numeric format ({target_val})...[/cyan]")
                    elif wants_lpa:
                        target_val = salaries["expected_lpa"] if is_expected else salaries["current_lpa"]
                        console.print(f"  [cyan]🔧 Auto-resolving salary error: switching to LPA format ({target_val})...[/cyan]")
                    else:
                        target_val = salaries["expected_full"] if is_expected else salaries["current_full"]
                        console.print(f"  [cyan]🔧 Auto-resolving salary error: defaulting to full numeric format ({target_val})...[/cyan]")

                    self.page.evaluate("""({ f_name, target_val, is_expected }) => {
                        const invalids = Array.from(document.querySelectorAll(
                            "input[aria-invalid='true'], input[type='number'], input[type='text']"
                        ));
                        for (const inp of invalids) {
                            let label = '';
                            let p = inp.parentElement;
                            while (p && p.tagName !== 'BODY') {
                                const lbl = p.querySelector('label');
                                if (lbl) { label = (lbl.innerText || '').toLowerCase(); break; }
                                p = p.parentElement;
                            }
                            const isSalaryInput = label.includes('ctc') || label.includes('salary') || label.includes('compensation') || (inp.name || '').toLowerCase().includes('ctc') || (inp.id || '').toLowerCase().includes('ctc');
                            const curVal = (inp.value || '').trim();
                            const curNum = parseFloat(curVal);
                            
                            const matchesExpected = is_expected ? (label.includes('expected') || (!label.includes('current') && isSalaryInput)) : (label.includes('current') || (!label.includes('expected') && isSalaryInput));
                            
                            if (isSalaryInput && matchesExpected) {
                                inp.focus();
                                inp.value = '';
                                inp.value = target_val;
                                inp.dispatchEvent(new Event('input', { bubbles: true }));
                                inp.dispatchEvent(new Event('change', { bubbles: true }));
                                inp.dispatchEvent(new Event('blur', { bubbles: true }));
                                break;
                            } else if (inp.getAttribute('aria-invalid') === 'true' && (!isNaN(curNum) && curNum <= 150)) {
                                inp.focus();
                                inp.value = '';
                                inp.value = target_val;
                                inp.dispatchEvent(new Event('input', { bubbles: true }));
                                inp.dispatchEvent(new Event('change', { bubbles: true }));
                                inp.dispatchEvent(new Event('blur', { bubbles: true }));
                                break;
                            }
                        }
                    }""", {"f_name": f_name, "target_val": target_val, "is_expected": is_expected})

                # 3. Required numeric field (e.g. "Please enter a valid number")
                elif any(term in msg for term in ["number", "digit", "decimal", "whole number"]):
                    console.print(f"  [cyan]🔧 Auto-resolving numeric input error...[/cyan]")
                    self.page.evaluate("""() => {
                        const invalids = Array.from(document.querySelectorAll("input[aria-invalid='true'], input[type='number']"));
                        for (const inp of invalids) {
                            if (!inp.value || inp.value === '0') {
                                inp.value = '2';
                                inp.dispatchEvent(new Event('input', { bubbles: true }));
                                inp.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        }
                    }""")

                # 3. Radio button error (e.g. "Please make a selection")
                elif any(term in msg for term in ["selection", "select an option", "choose"]):
                    console.print(f"  [cyan]🔧 Auto-resolving unselected radio button...[/cyan]")
                    self.page.evaluate("""() => {
                        const fieldsets = Array.from(document.querySelectorAll("fieldset, [role='radiogroup']"));
                        for (const fs of fieldsets) {
                            const radios = Array.from(fs.querySelectorAll("input[type='radio']"));
                            const hasChecked = radios.some(r => r.checked);
                            if (!hasChecked && radios.length > 0) {
                                let target = radios[0];
                                for (const r of radios) {
                                    if ((r.value || '').toLowerCase() === 'yes') { target = r; break; }
                                }
                                target.checked = true;
                                target.dispatchEvent(new Event('change', { bubbles: true }));
                                target.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                            }
                        }
                    }""")

            # Check if errors remain after healing
            self.browser_mgr.sleep(0.5, 1.0)
            remaining_errors = self.page.evaluate("""() => {
                const errElements = Array.from(document.querySelectorAll(
                    ".artdeco-inline-feedback--error, [data-test-form-builder-error], p[class*='feedback--error']"
                ));
                return errElements.map(el => (el.innerText || '').trim()).filter(Boolean);
            }""")

            if remaining_errors:
                return True, "; ".join(remaining_errors)
            return False, ""
        except Exception as e:
            logger.debug(f"Error in _detect_and_resolve_errors: {e}")
            return False, ""

    def _resolve_radio_choice(self, question: str, options: List[str]) -> Optional[str]:
        q = question.lower()

        # Interview / Availability
        if any(term in q for term in ["interview", "face-to-face", "onsite interview", "available for", "immediate"]):
            return self._match_option(["yes"], options) or "Yes"

        # Work authorization
        if any(term in q for term in ["authorized to work", "legally authorized", "eligible to work", "work in india"]):
            return self._match_option(["yes", "authorized"], options) or "Yes"

        # Sponsorship
        if any(term in q for term in ["sponsorship", "visa sponsorship", "require sponsorship"]):
            return self._match_option(["no", "do not require"], options) or "No"

        # Relocation
        if "relocate" in q:
            return self._match_option(["yes"], options) or "Yes"

        # Commute / On-site / Hybrid
        if any(term in q for term in ["commute", "hybrid", "on-site", "in-office"]):
            return self._match_option(["yes"], options) or "Yes"

        # Background check / Drug test
        if any(term in q for term in ["background", "drug test"]):
            return self._match_option(["yes"], options) or "Yes"

        # EEO Gender
        if "gender" in q:
            ans = self.screening_cfg.gender
            return self._match_option([ans.lower(), "decline", "prefer not"], options)

        # Current / Expected CTC Radio options
        if any(term in q for term in ["ctc", "salary", "compensation"]):
            salaries = self._get_salary_values()
            is_expected = any(term in q for term in ["expected", "desired", "target"])
            target_lpa = salaries["expected_lpa"] if is_expected else salaries["current_lpa"]
            target_full = salaries["expected_full"] if is_expected else salaries["current_full"]
            matched = self._match_option([
                f"{target_lpa} lpa", target_lpa,
                target_full, "10,00,000" if not is_expected else "16,00,000"
            ], options)
            if matched:
                return matched

        # Fallback to AI
        ans = answer_screening_question(
            question=question,
            field_type="choice",
            options=options,
            candidate=self.cand,
            ai_config=self.ai_cfg
        )
        return self._match_option([ans.lower()], options)

    def _resolve_dropdown_choice(self, question: str, options: List[str]) -> Optional[str]:
        q = question.lower()
        if not options:
            return None

        # Phone Country Code (e.g. India (+91))
        if any(term in q for term in ["country code", "phone country", "dial code", "phone code"]):
            target_country = self.cand.country.lower()
            code = self.cand.phone_country_code.replace("+", "").strip()
            # 1. Match both country name and code: "India (+91)"
            for opt in options:
                opt_l = opt.lower()
                if target_country in opt_l and (f"+{code}" in opt_l or f"({code})" in opt_l):
                    return opt
            # 2. Match +91 or country
            for opt in options:
                opt_l = opt.lower()
                if f"+{code}" in opt_l or target_country in opt_l:
                    return opt
            return self._match_option([self.cand.country, "india", "+91", "91"], options)

        # Country
        if "country" in q and not any(term in q for term in ["code", "dial"]):
            return self._match_option([self.cand.country, "india"], options) or options[0]

        # State / Province
        if any(term in q for term in ["state", "province", "region"]):
            return self._match_option([self.cand.state, "karnataka"], options) or options[0]

        # Gender
        if "gender" in q or "sex" in q:
            return self._match_option([self.screening_cfg.gender, "male", "decline"], options) or options[0]

        # Disability
        if "disability" in q:
            return self._match_option(["no", "do not have", "decline"], options) or options[0]

        # Veteran
        if "veteran" in q:
            return self._match_option(["no", "not a protected", "decline"], options) or options[0]

        # Sponsorship
        if "sponsorship" in q:
            return self._match_option(["no", "will not require", "do not require"], options) or options[0]

        # Work Authorization
        if any(term in q for term in ["authorized", "authorization", "eligible to work", "legally"]):
            return self._match_option(["yes", "authorized", "eligible"], options) or options[0]

        # Education
        if any(term in q for term in ["education", "degree"]):
            return self._match_option(["bachelor", "undergraduate", "degree", "b.e", "b.tech"], options) or options[0]

        # Notice Period
        if "notice period" in q:
            days = self.screening_cfg.notice_period_days
            months = days // 30
            candidates = [
                str(days),
                f"{days} days",
                f"{days} day",
                f"{months} month" if months == 1 else f"{months} months",
                self.screening_cfg.notice_period_text,
            ]
            return self._match_option(candidates, options) or options[0]

        # English proficiency
        if "english" in q or "proficiency" in q:
            return self._match_option(["professional", "fluent", "native", "advanced"], options) or options[0]

        # Current / Expected CTC Dropdown
        if any(term in q for term in ["ctc", "salary", "compensation"]):
            salaries = self._get_salary_values()
            is_expected = any(term in q for term in ["expected", "desired", "target"])
            target_lpa = salaries["expected_lpa"] if is_expected else salaries["current_lpa"]
            target_full = salaries["expected_full"] if is_expected else salaries["current_full"]
            matched = self._match_option([
                f"{target_lpa} lpa", target_lpa,
                target_full, "10,00,000" if not is_expected else "16,00,000"
            ], options)
            if matched:
                return matched

        # Fallback to AI
        ans = answer_screening_question(
            question=question,
            field_type="choice",
            options=options,
            candidate=self.cand,
            ai_config=self.ai_cfg
        )
        return self._match_option([ans.lower()], options) or options[0]

    def _get_salary_values(self) -> Dict[str, str]:
        """
        Returns structured salary values in both LPA and full integer format.
        Reads custom configuration or auto-calculates from LPA values.
        """
        # Current CTC
        c_lpa_raw = str(getattr(self.screening_cfg, "current_ctc_lpa", "10")).lower().replace("lpa", "").replace("lakhs", "").strip()
        try:
            c_lpa_float = float(c_lpa_raw)
            c_lpa_clean = str(int(c_lpa_float) if c_lpa_float.is_integer() else c_lpa_float)
            c_full_calc = str(int(c_lpa_float * 100000))
        except Exception:
            c_lpa_clean = "10"
            c_full_calc = "1000000"

        c_full_cfg = getattr(self.screening_cfg, "current_ctc_full", None)
        c_full_clean = str(c_full_cfg).replace(",", "").strip() if c_full_cfg else c_full_calc

        # Expected CTC
        e_lpa_raw = str(getattr(self.screening_cfg, "expected_ctc_lpa", "16")).lower().replace("lpa", "").replace("lakhs", "").strip()
        try:
            e_lpa_float = float(e_lpa_raw)
            e_lpa_clean = str(int(e_lpa_float) if e_lpa_float.is_integer() else e_lpa_float)
            e_full_calc = str(int(e_lpa_float * 100000))
        except Exception:
            e_lpa_clean = "16"
            e_full_calc = "1600000"

        e_full_cfg = getattr(self.screening_cfg, "expected_ctc_full", None)
        e_full_clean = str(e_full_cfg).replace(",", "").strip() if e_full_cfg else e_full_calc

        return {
            "current_lpa": c_lpa_clean,
            "current_full": c_full_clean,
            "expected_lpa": e_lpa_clean,
            "expected_full": e_full_clean,
        }

    def _resolve_salary(self, question: str, inp: Optional[Locator] = None) -> Optional[str]:
        """
        Intelligently resolves Current CTC or Expected CTC screening questions.
        Determines whether the form requires:
        - LPA format (e.g. '10' or '16')
        - Full numeric format (e.g. '1000000' or '1600000')
        - Monthly salary format (e.g. '83333' or '133333')
        Inspects question wording, helper/container text, placeholder, min/max HTML attributes.
        """
        q_lower = question.lower()
        is_salary = any(term in q_lower for term in [
            "ctc", "salary", "compensation", "remuneration", "package", "fixed pay", "gross pay"
        ])
        if not is_salary:
            return None

        # Check for bonus / variable pay questions
        if any(term in q_lower for term in ["variable", "bonus", "incentive"]):
            return "0"

        salaries = self._get_salary_values()

        # Determine if Current or Expected
        is_expected = any(term in q_lower for term in ["expected", "expectation", "desired", "target", "quote", "looking for"])
        is_current = any(term in q_lower for term in ["current", "present", "existing", "drawn", "fixed", "base"])

        if is_expected and not is_current:
            target_type = "expected"
        elif is_current and not is_expected:
            target_type = "current"
        elif is_expected:
            target_type = "expected"
        else:
            target_type = "current"

        # Inspect DOM attributes & enclosing container text via JS
        placeholder = ""
        min_val = None
        max_val = None
        extra_text = ""
        if inp:
            try:
                info = inp.evaluate("""el => {
                    let helper = '';
                    let p = el.parentElement;
                    let depth = 0;
                    while (p && p.tagName !== 'BODY' && depth < 4) {
                        const t = (p.innerText || '').trim();
                        if (t.length > helper.length && t.length < 400) {
                            helper = t;
                        }
                        p = p.parentElement;
                        depth++;
                    }
                    return {
                        placeholder: el.getAttribute('placeholder') || el.getAttribute('aria-placeholder') || '',
                        min: el.getAttribute('min'),
                        max: el.getAttribute('max'),
                        helper: helper
                    };
                }""")
                placeholder = (info.get("placeholder") or "").lower()
                extra_text = (info.get("helper") or "").lower()
                if info.get("min") is not None:
                    try: min_val = float(info["min"])
                    except Exception: pass
                if info.get("max") is not None:
                    try: max_val = float(info["max"])
                    except Exception: pass
            except Exception:
                pass

        context = f"{q_lower} {extra_text} {placeholder}"

        # 1. Check for monthly salary request
        if any(term in context for term in ["monthly", "per month", "p.m.", "p.m", "/month"]):
            if target_type == "expected":
                monthly_val = str(int(float(salaries["expected_full"]) / 12))
            else:
                monthly_val = str(int(float(salaries["current_full"]) / 12))
            logger.info(f"Resolved monthly salary for '{question}': '{monthly_val}'")
            return monthly_val

        # 2. Check for explicit full number cues vs LPA cues
        # Clean phrases like "instead of 10lpa" or "not in lpa" so they don't falsely trigger LPA detection
        context_clean = re.sub(r'instead\s+of\s+[0-9]*\s*lpa', '', context)
        context_clean = re.sub(r'not\s+in\s+lpa', '', context_clean)

        has_lpa_phrase = any(term in context_clean for term in [
            "in lpa", "(lpa)", "/lpa", "in lakhs", "in lac", "in lacs", "(in lakhs)", "(in lac)", "lakhs per annum", "lacs per annum"
        ]) or bool(re.search(r'\blpa\b', context_clean))

        wants_full_number = False

        # HTML min attribute (e.g. min="100000" or min="50000") is a 100% guarantee of full digits
        if min_val is not None and min_val >= 1000:
            wants_full_number = True

        # Placeholder checks (e.g., "e.g. 1000000", "10,00,000", "500000")
        if any(p_cue in placeholder for p_cue in ["000", "00,000", "500000", "1000000"]):
            wants_full_number = True

        # Text cues for full numeric entry (e.g. "enter in full numbers eg, 10,00,000 instead of 10LPA", "Annual in INR", "Per Annum")
        full_text_cues = [
            "full number", "in full", "whole number", "full digits", "without decimal",
            "10,00,000", "1000000", "500000", "16,00,000", "1600000",
            "annual", "per annum", "p.a.", "p.a", "/year", "per year", "yearly",
            "in inr", "(in inr)", "in rs", "(in rs)", "in rupees", "(in rupees)",
            "gross salary", "gross compensation", "annual compensation", "exact amount"
        ]
        if any(cue in context for cue in full_text_cues) and not has_lpa_phrase:
            wants_full_number = True

        # If explicit LPA indicators exist or max <= 150, force LPA
        if has_lpa_phrase or (max_val is not None and max_val <= 150):
            wants_full_number = False

        # Return resolved answer
        if target_type == "expected":
            chosen = salaries["expected_full"] if wants_full_number else salaries["expected_lpa"]
            logger.info(f"Resolved expected salary for '{question}': '{chosen}' (format={'full' if wants_full_number else 'lpa'})")
            return chosen
        else:
            chosen = salaries["current_full"] if wants_full_number else salaries["current_lpa"]
            logger.info(f"Resolved current salary for '{question}': '{chosen}' (format={'full' if wants_full_number else 'lpa'})")
            return chosen

    def _resolve_text_input(self, question: str, is_numeric: bool, inp: Optional[Locator] = None) -> str:
        q = question.lower()

        # 1. Years of experience with a specific skill/tool
        if any(term in q for term in ["years of experience", "how many years", "years with", "experience with"]):
            for skill, yrs in self.cand.skills_experience.items():
                if skill.lower() in q:
                    return str(int(yrs) if yrs.is_integer() else yrs)
            default_yrs = self.cand.skills_experience.get("default", self.cand.overall_experience_years)
            return str(int(default_yrs) if default_yrs.is_integer() else default_yrs)

        # Total / Overall Experience
        if any(term in q for term in ["total experience", "overall experience", "overall years"]):
            ovr = self.cand.overall_experience_years
            return str(int(ovr) if ovr.is_integer() else ovr)

        # 2. Notice period (use numeric digits e.g. '60' or '30' to pass digit validations)
        if "notice period" in q:
            return str(self.screening_cfg.notice_period_days)

        # 3. Current / Expected CTC (smart auto-detection between LPA and full numbers)
        if any(term in q for term in ["ctc", "salary", "compensation", "remuneration", "package"]):
            sal_ans = self._resolve_salary(question, inp=inp)
            if sal_ans:
                return sal_ans

        # 4. GPA / Percentage
        if "gpa" in q or "percentage" in q:
            return self.cand.gpa

        # 5. City / Location
        if "city" in q:
            return self.cand.city
        if "postal" in q or "zip" in q or "pin" in q:
            return self.cand.postal_code

        # 6. Links
        if "linkedin" in q:
            return self.cand.linkedin_url
        if "github" in q:
            return self.cand.github_url
        if "portfolio" in q or "website" in q:
            return self.cand.portfolio_url or self.cand.github_url

        # Fallback to AI
        return answer_screening_question(
            question=question,
            field_type="numeric" if is_numeric else "text",
            candidate=self.cand,
            ai_config=self.ai_cfg
        )

    def _match_option(self, targets: List[str], options: List[str]) -> Optional[str]:
        for target in targets:
            for opt in options:
                if target.lower() == opt.strip().lower():
                    return opt.strip()
            for opt in options:
                if target.lower() in opt.lower():
                    return opt.strip()
        return None

    def _discard_modal(self, modal: Locator):
        """Clicks the modal dismiss (X) button and confirms 'Discard'."""
        try:
            dismiss_selectors = [
                "button[aria-label*='Dismiss']",
                "button[aria-label*='Close']",
                "button.artdeco-modal__dismiss",
                "button[data-test-modal-close-btn]",
                "div[role='dialog'] button:has(svg)"
            ]
            for sel in dismiss_selectors:
                btn = self.page.locator(sel)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    self.browser_mgr.sleep(0.8, 1.5)
                    break

            discard_btn = self.page.locator("button[data-control-name='discard_application_confirm_btn'], button:has-text('Discard')")
            if discard_btn.count() > 0 and discard_btn.first.is_visible():
                discard_btn.first.click()
                self.browser_mgr.sleep(0.8, 1.5)
        except Exception as e:
            logger.debug(f"Error discarding modal: {e}")
