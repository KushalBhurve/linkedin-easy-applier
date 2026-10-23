import re
import logging
from typing import Tuple, Optional
from models import SuitabilityFilterConfig, JobCard, CandidateConfig, AIFallbackConfig
from tracker import ApplicationTracker

logger = logging.getLogger("linkedin_easy_applier.filter")

class JobSuitabilityFilter:
    def __init__(
        self,
        filter_cfg: SuitabilityFilterConfig,
        tracker: ApplicationTracker,
        candidate: Optional[CandidateConfig] = None,
        ai_config: Optional[AIFallbackConfig] = None
    ):
        self.cfg = filter_cfg
        self.tracker = tracker
        self.candidate = candidate
        self.ai_config = ai_config

    def check_job_card(self, job: JobCard) -> Tuple[bool, str]:
        """
        Quick evaluation based on job title, company name, and application history.
        Returns (is_suitable, reason).
        """
        # 1. Already processed / applied check
        if self.tracker.is_already_applied(job.job_id):
            return False, f"Already applied or processed earlier (ID: {job.job_id})"

        # 2. Company blacklist check
        lower_company = job.company.lower().strip()
        blacklist = list(self.cfg.company_blacklist)
        if self.candidate and getattr(self.candidate, "current_company", None):
            curr = self.candidate.current_company.strip()
            if curr and curr.lower() not in [b.lower() for b in blacklist]:
                blacklist.append(curr)
            curr_lower = curr.lower()
            if "tcs" in curr_lower or "tata consultancy" in curr_lower:
                for alias in ["Tata Consultancy Services", "TCS", "Tata Consultancy"]:
                    if alias.lower() not in [b.lower() for b in blacklist]:
                        blacklist.append(alias)

        for blacklisted in blacklist:
            b = blacklisted.lower().strip()
            if not b:
                continue
            if len(b) <= 4:
                pattern = rf"\b{re.escape(b)}\b"
                if re.search(pattern, lower_company):
                    return False, f"Company '{job.company}' matches blacklist filter '{blacklisted}'"
            elif b in lower_company:
                return False, f"Company '{job.company}' matches blacklist filter '{blacklisted}'"

        # 3. Seniority exclusion in title
        if self.cfg.exclude_senior_titles:
            lower_title = job.title.lower()
            for keyword in self.cfg.senior_keywords:
                # Match word boundary to avoid false positives (e.g., 'lead' in 'leadership')
                pattern = rf"\b{re.escape(keyword.lower())}\b"
                if re.search(pattern, lower_title):
                    return False, f"Job title '{job.title}' contains senior keyword '{keyword}'"

        # 4. Strict Location Check (Allowed: Remote anywhere, OR On-site / Hybrid in Bangalore)
        if getattr(self.cfg, "enforce_location", True):
            loc_lower = (job.location or "").lower()
            title_lower = (job.title or "").lower()

            is_remote = any(term in loc_lower or term in title_lower for term in [
                "remote", "work from home", "wfh", "anywhere", "virtual", "telecommute"
            ])

            if is_remote:
                if not getattr(self.cfg, "allow_remote", True):
                    return False, f"Remote roles excluded by filter ({job.location})"
            else:
                allowed_cities = getattr(self.cfg, "allowed_locations", ["bengaluru", "bangalore", "karnataka"])
                is_bangalore = any(city.lower() in loc_lower for city in allowed_cities)

                # Disallowed cities outside Bangalore
                disallowed = [
                    "hyderabad", "secunderabad", "telangana",
                    "pune", "mumbai", "navi mumbai", "thane", "maharashtra",
                    "gurgaon", "gurugram", "noida", "greater noida", "delhi", "new delhi", "ncr", "haryana",
                    "chennai", "tamil nadu", "coimbatore",
                    "kolkata", "west bengal",
                    "ahmedabad", "gujarat", "vadodara",
                    "chandigarh", "mohali", "punjab",
                    "jaipur", "rajasthan",
                    "indore", "bhopal", "madhya pradesh",
                    "kochi", "trivandrum", "kerala",
                    "lucknow", "bhubaneswar", "visakhapatnam"
                ]

                # If location or title mentions a city outside Bangalore and is not remote, reject immediately
                for d_city in disallowed:
                    if d_city in loc_lower or d_city in title_lower:
                        return False, f"Job requires on-site/hybrid work in {d_city.title()} (outside Bangalore)"

                # If not in Bangalore and not generic 'india', reject
                if not is_bangalore and loc_lower != "india":
                    return False, f"Job location '{job.location}' is outside Bangalore and not Remote"

        return True, "Job card passed initial suitability filter"

    def check_job_description(self, job_title: str, description_text: str) -> Tuple[bool, str]:
        """
        Deep evaluation of the job description text against candidate's profile,
        focusing on years of experience caps, seniority level, and location requirements.
        """
        if not description_text:
            return True, "No description available, continuing"

        # Normalize special unicode dashes, en-dashes, and control characters
        text = description_text.lower().replace('\u2013', '-').replace('\u2014', '-').replace('\u2212', '-')
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\xff]', ' ', text)

        # 1. Location verification within JD body (disqualifies on-site/hybrid roles outside Bangalore)
        if getattr(self.cfg, "enforce_location", True):
            is_jd_remote = any(term in text for term in [
                "100% remote", "fully remote", "remote work", "work from home", "wfh",
                "remote-first", "remote role", "remote position", "work from anywhere"
            ])
            is_jd_bangalore = any(term in text for term in [
                "bangalore", "bengaluru", "karnataka"
            ])

            # If not explicitly remote and not in Bangalore, check for required on-site/hybrid work in other cities
            if not is_jd_remote and not is_jd_bangalore:
                outside_cities = [
                    "hyderabad", "pune", "mumbai", "gurgaon", "gurugram", "noida",
                    "delhi", "chennai", "kolkata", "ahmedabad", "kochi", "chandigarh"
                ]
                for city in outside_cities:
                    patterns = [
                        rf"location\s*:\s*{city}",
                        rf"office\s*(?:in|at)\s*{city}",
                        rf"based\s*(?:in|out of)\s*{city}",
                        rf"relocate\s*to\s*{city}",
                        rf"work\s*from\s*(?:our\s*)?{city}",
                        rf"{city}\s*(?:office|campus|location|branch|site)"
                    ]
                    if any(re.search(p, text) for p in patterns):
                        return False, f"JD indicates on-site/hybrid work in {city.title()} (outside Bangalore)"

        # 2. Range pattern check (e.g., "4-6 years", "5 to 8 yrs", "3-5 years")
        range_pats = [
            r'(\d+)\s*(?:-|to)\s*(\d+)\+?\s*(?:years|yrs)',
            r'experience\s*:\s*(\d+)\s*(?:-|to)\s*(\d+)\s*(?:years|yrs)?',
            r'(\d+)\s*(?:-|to)\s*(\d+)\s*(?:years|yrs)(?:\s*of)?\s*(?:relevant|overall|work|experience)'
        ]
        for pat in range_pats:
            for m in re.finditer(pat, text):
                min_y, max_y = int(m.group(1)), int(m.group(2))
                if min_y > self.cfg.max_years_required:
                    return False, f"JD requires {min_y}-{max_y} years experience (Candidate cap: {self.cfg.max_years_required} yrs)"

        # 3. Plus or single-number requirements (e.g., "5+ years", "minimum 4 years", "at least 5 yrs")
        single_pats = [
            r'(\d+)\+?\s*(?:years|yrs)(?:\s*of)?\s*(?:relevant|overall|total|work|industry|hands-on|professional)?\s*experience',
            r'(?:minimum|at\s*least|min\.?)\s*(?:of\s*)?(\d+)\+?\s*(?:years|yrs)',
            r'(\d+)\+\s*(?:years|yrs)',
            r'experience\s*[:\-]\s*(\d+)\+?\s*(?:years|yrs)'
        ]
        for pat in single_pats:
            for m in re.finditer(pat, text):
                y = int(m.group(1))
                if y > self.cfg.max_years_required:
                    return False, f"JD specifies {y}+ years experience requirement (Candidate cap: {self.cfg.max_years_required} yrs)"

        # 4. Senior role in JD body
        if self.cfg.exclude_senior_titles:
            senior_in_body_patterns = [
                r"seeking\s+a\s+(?:lead|principal|staff|architect|director|vp)",
                r"looking\s+for\s+a\s+(?:lead|principal|staff|architect|director|vp)",
                r"role\s+as\s+a\s+(?:lead|principal|staff|architect|director|vp)"
            ]
            for s_pat in senior_in_body_patterns:
                if re.search(s_pat, text):
                    return False, "Job description specifies a senior/lead leadership role"

        # 5. AI-Powered Deep Evaluation (if AI is enabled in config)
        if self.ai_config and self.ai_config.enabled:
            try:
                from llm_helper import evaluate_job_suitability_with_ai
                ai_suitable, ai_reason = evaluate_job_suitability_with_ai(
                    job_title=job_title,
                    job_description=description_text,
                    candidate=self.candidate,
                    max_years_required=self.cfg.max_years_required,
                    ai_config=self.ai_config
                )
                if not ai_suitable:
                    return False, f"AI Evaluation: {ai_reason}"
            except Exception as e:
                logger.debug(f"AI JD evaluation skipped: {e}")

        return True, "Job description passed experience and suitability check"
