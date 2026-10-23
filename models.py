from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field

class CandidateConfig(BaseModel):
    first_name: str = "Kushal"
    last_name: str = "B R"
    full_name: str = "Kushal B R"
    email: str = "kushal@example.com"
    phone_country_code: str = "+91"
    phone: str = "9876543210"
    city: str = "Bengaluru"
    state: str = "Karnataka"
    country: str = "India"
    postal_code: str = "560001"
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    current_title: str = "AI & Platform Engineer"
    current_company: str = "TCS"
    overall_experience_years: float = 2.0
    highest_education: str = "Bachelor's Degree"
    university: str = "Atria Institute of Technology"
    major: str = "Computer Science and Engineering"
    gpa: str = "8.5"
    resume_path: str = "resume.pdf"
    skills_experience: Dict[str, float] = Field(default_factory=dict)

class ScreeningAnswersConfig(BaseModel):
    work_authorization: str = "Yes"
    sponsorship_required: str = "No"
    over_18: str = "Yes"
    background_check_consent: str = "Yes"
    drug_test_consent: str = "Yes"
    willing_to_relocate: str = "Yes"
    comfort_with_commute: str = "Yes"
    notice_period_days: int = 60
    notice_period_text: str = "60 Days"
    current_ctc_lpa: str = "10"
    expected_ctc_lpa: str = "16"
    current_ctc_full: Optional[str] = "1000000"
    expected_ctc_full: Optional[str] = "1600000"
    currency: str = "INR"
    gender: str = "Male"
    veteran_status: str = "No"
    disability_status: str = "No"

class SearchConfig(BaseModel):
    roles: List[str] = Field(default_factory=lambda: ["AI Engineer", "GenAI Engineer", "Python Developer"])
    locations: List[str] = Field(default_factory=lambda: ["Bengaluru", "Remote"])
    location: str = "Bengaluru"
    workplace_types: List[int] = Field(default_factory=lambda: [1, 2, 3])
    date_posted: str = "past_week"
    experience_levels: List[int] = Field(default_factory=lambda: [2, 3])
    easy_apply_only: bool = True

class SuitabilityFilterConfig(BaseModel):
    max_years_required: int = 3
    exclude_senior_titles: bool = True
    senior_keywords: List[str] = Field(default_factory=list)
    company_blacklist: List[str] = Field(default_factory=list)
    enforce_location: bool = True
    allowed_locations: List[str] = Field(default_factory=lambda: ["Bengaluru", "Bangalore", "Karnataka"])
    allow_remote: bool = True

class ExecutionConfig(BaseModel):
    dry_run: bool = True
    headless: bool = False
    min_delay_sec: float = 2.0
    max_delay_sec: float = 5.0
    delay_between_jobs_sec: float = 12.0
    max_applications_per_run: int = 15
    save_screenshots: bool = True

class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    model: str = "llama3.2:latest"

class GroqConfig(BaseModel):
    api_key: str = ""
    model: str = "llama-3.3-70b-versatile"

class AIFallbackConfig(BaseModel):
    enabled: bool = True
    provider: str = "ollama"
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    groq: GroqConfig = Field(default_factory=GroqConfig)

class AppConfig(BaseModel):
    candidate: CandidateConfig
    screening_answers: ScreeningAnswersConfig = Field(default_factory=ScreeningAnswersConfig)
    search: SearchConfig
    suitability_filter: SuitabilityFilterConfig
    execution: ExecutionConfig
    ai_fallback: AIFallbackConfig

class JobCard(BaseModel):
    job_id: str
    title: str
    company: str
    location: str
    job_url: str
    is_easy_apply: bool = True

class ApplicationResult(BaseModel):
    job_id: str
    title: str
    company: str
    location: str
    job_url: str
    status: str  # 'applied', 'dry_run_success', 'skipped_senior', 'skipped_company', 'failed_form', 'already_applied'
    reason: str = ""
    screening_log: Dict[str, Any] = Field(default_factory=dict)
    screenshot_path: Optional[str] = None
    applied_at: str
