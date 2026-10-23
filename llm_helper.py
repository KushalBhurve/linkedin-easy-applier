import logging
import json
from typing import List, Optional, Any, Dict, Tuple
import requests

from models import AIFallbackConfig, CandidateConfig

logger = logging.getLogger("linkedin_easy_applier.llm")

SYSTEM_PROMPT = """You are an intelligent assistant helping a job candidate accurately fill out LinkedIn job application screening questions.
Candidate Background:
- Full Name: {full_name}
- Current Title: {current_title} ({current_company})
- Overall Experience: {overall_experience_years} years
- Education: {highest_education} in {major} from {university} (GPA: {gpa})
- Location: {city}, {state}, {country}
- Key Skills: Python, AI/GenAI, RAG, LangGraph, LangChain, GCP, Cloud Run, Java, Spring Boot, Node.js, SQL, Docker.

CRITICAL INSTRUCTIONS:
1. Provide ONLY the final concise answer requested. Do NOT include any explanations, markdown, or commentary.
2. For numeric fields (years of experience, notice period, CTC): Output ONLY a clean single number (e.g., '2' or '30' or '12').
3. For boolean/radio questions: Output ONLY 'Yes' or 'No'.
4. For multiple choice / dropdown questions: Output EXACTLY one of the provided options word-for-word.
"""

def answer_screening_question(
    question: str,
    field_type: str,  # 'numeric', 'boolean', 'choice', 'text'
    options: Optional[List[str]] = None,
    candidate: Optional[CandidateConfig] = None,
    ai_config: Optional[AIFallbackConfig] = None
) -> str:
    """
    Uses an LLM (Ollama or Groq) to intelligently generate a truthful, high-quality answer
    for custom screening questions.
    """
    if not ai_config or not ai_config.enabled:
        return _fallback_default(field_type, options, candidate)

    # Format Candidate details into prompt
    cand_info = {
        "full_name": candidate.full_name if candidate else "Kushal B R",
        "current_title": candidate.current_title if candidate else "AI & Platform Engineer",
        "current_company": candidate.current_company if candidate else "TCS",
        "overall_experience_years": candidate.overall_experience_years if candidate else 2.0,
        "highest_education": candidate.highest_education if candidate else "Bachelor's Degree",
        "major": candidate.major if candidate else "Computer Science",
        "university": candidate.university if candidate else "Atria Institute of Technology",
        "gpa": candidate.gpa if candidate else "8.5",
        "city": candidate.city if candidate else "Bengaluru",
        "state": candidate.state if candidate else "Karnataka",
        "country": candidate.country if candidate else "India"
    }

    # Format skills experience
    skills_exp_str = ""
    if candidate and candidate.skills_experience:
        skills_exp_str = "\n- Specific Skill Experience: " + ", ".join(f"{k}: {v} yrs" for k, v in candidate.skills_experience.items())

    formatted_sys = SYSTEM_PROMPT.format(**cand_info) + skills_exp_str

    user_prompt = f"Question: {question}\nField Type: {field_type}\n"
    if options:
        user_prompt += f"Available Options: {json.dumps(options)}\n"
    user_prompt += "Your concise answer:"

    try:
        if ai_config.provider.lower() == "ollama":
            import ollama
            client = ollama.Client(host=ai_config.ollama.base_url, timeout=12)
            response = client.chat(
                model=ai_config.ollama.model,
                messages=[
                    {"role": "system", "content": formatted_sys},
                    {"role": "user", "content": user_prompt}
                ],
                options={"temperature": 0.1}
            )
            raw = response["message"]["content"].strip()
            return _clean_llm_response(raw, field_type, options)

        elif ai_config.provider.lower() == "groq" and ai_config.groq.api_key:
            headers = {
                "Authorization": f"Bearer {ai_config.groq.api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": ai_config.groq.model,
                "messages": [
                    {"role": "system", "content": formatted_sys},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.1,
                "max_tokens": 100
            }
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=10
            )
            res.raise_for_status()
            raw = res.json()["choices"][0]["message"]["content"].strip()
            return _clean_llm_response(raw, field_type, options)

    except Exception as e:
        logger.warning(f"AI question resolution failed for '{question}': {e}. Using fallback default.")

    return _fallback_default(field_type, options, candidate)


def _clean_llm_response(raw: str, field_type: str, options: Optional[List[str]]) -> str:
    cleaned = raw.strip().strip('"').strip("'")
    
    # If options are provided, match the closest one
    if options:
        # Exact match
        for opt in options:
            if opt.strip().lower() == cleaned.lower():
                return opt.strip()
        # Substring match
        for opt in options:
            if opt.strip().lower() in cleaned.lower() or cleaned.lower() in opt.strip().lower():
                return opt.strip()
        return options[0]

    if field_type == "numeric":
        import re
        match = re.search(r"[-+]?\d*\.\d+|\d+", cleaned)
        if match:
            return match.group(0)
        return "2"

    if field_type == "boolean":
        lower = cleaned.lower()
        if "yes" in lower:
            return "Yes"
        if "no" in lower:
            return "No"
        return "Yes"

    return cleaned


def _fallback_default(field_type: str, options: Optional[List[str]], candidate: Optional[CandidateConfig]) -> str:
    if options and len(options) > 0:
        # Check for standard positive options
        for opt in options:
            if opt.lower() in ["yes", "authorized", "bachelor's", "immediate", "full-time", "professional"]:
                return opt
        return options[0]
        
    if field_type == "numeric":
        return str(int(candidate.overall_experience_years)) if candidate else "2"
    if field_type == "boolean":
        return "Yes"
    if candidate:
        return candidate.full_name
    return "Yes"


def evaluate_job_suitability_with_ai(
    job_title: str,
    job_description: str,
    candidate: Optional[CandidateConfig],
    max_years_required: int,
    ai_config: Optional[AIFallbackConfig]
) -> Tuple[bool, str]:
    """
    Uses Ollama or Groq to evaluate whether a Job Description matches candidate's profile,
    specifically checking required years of experience and seniority expectations.
    Returns (is_suitable, reason).
    """
    if not ai_config or not ai_config.enabled or not job_description:
        return True, "AI evaluation skipped (disabled or no JD)"

    cand_title = candidate.current_title if candidate else "AI & Platform Engineer"
    cand_yoe = candidate.overall_experience_years if candidate else 2.0
    skills_list = ", ".join(candidate.skills_experience.keys()) if candidate and candidate.skills_experience else "Python, GenAI, LLMs, RAG, GCP"

    prompt = f"""Candidate Profile:
- Current Title: {cand_title}
- Total Work Experience: {cand_yoe} years
- Maximum Acceptable Requirement Cap: {max_years_required} years
- Core Skills: {skills_list}

Job Title: {job_title}
Job Description (excerpt):
{job_description[:2000]}

Task:
Evaluate whether this candidate qualifies for this role, focusing strictly on:
1. Experience Level: Reject if the role explicitly demands more than {max_years_required} years of experience (e.g. 4+, 5+, 7+ years).
2. Seniority Level: Reject if it is a senior leadership role (Staff, Principal, Director, Lead Architect) inappropriate for a {cand_yoe} year candidate.

Respond ONLY with valid JSON in this exact structure:
{{"suitable": true, "reason": "matches 1-3 years requirement"}} or
{{"suitable": false, "reason": "requires 5+ years of experience"}}"""

    try:
        raw_json = ""
        if ai_config.provider.lower() == "ollama":
            import ollama
            client = ollama.Client(host=ai_config.ollama.base_url, timeout=6)
            res = client.chat(
                model=ai_config.ollama.model,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.1}
            )
            raw_json = res["message"]["content"].strip()
        elif ai_config.provider.lower() == "groq" and ai_config.groq.api_key:
            headers = {
                "Authorization": f"Bearer {ai_config.groq.api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": ai_config.groq.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 120
            }
            res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=6)
            res.raise_for_status()
            raw_json = res.json()["choices"][0]["message"]["content"].strip()

        # Parse JSON
        import re
        m = re.search(r"\{.*\}", raw_json, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            is_suitable = bool(data.get("suitable", True))
            reason = str(data.get("reason", "AI evaluation"))
            return is_suitable, reason
    except Exception as e:
        logger.debug(f"AI JD suitability evaluation failed: {e}")

    return True, "AI check defaulted to true"

