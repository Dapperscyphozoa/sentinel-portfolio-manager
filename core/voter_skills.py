"""Voter skill profiles + domain classification.

Initial values are HEURISTIC priors based on each model's training emphasis
and benchmark performance. The skill_ledger module (separate) progressively
updates these from real ratings.

Domains: quant, code, research, market, strategy, meta, creative, factual, risk
"""
from __future__ import annotations

from typing import Optional


# ─────────────────── Voter skill profiles (heuristic priors) ───────────────────
# Score 0.0-1.0 per domain. Built from public benchmark data + known training emphasis.
VOTER_PROFILES: dict[str, dict[str, float]] = {
    "cerebras-qwen3-235b": {
        # Qwen-3-235B: strong math/reasoning, instruction-tuned 2507
        "quant": 0.92, "code": 0.85, "research": 0.88, "market": 0.78,
        "strategy": 0.80, "meta": 0.85, "creative": 0.65, "factual": 0.80,
        "risk": 0.82,
    },
    "cerebras-gpt-oss-120b": {
        # OpenAI's open model on Cerebras — generalist, very fast
        "quant": 0.82, "code": 0.85, "research": 0.85, "market": 0.78,
        "strategy": 0.80, "meta": 0.80, "creative": 0.78, "factual": 0.85,
        "risk": 0.78,
    },
    "cerebras-glm-4.7": {
        # GLM-4.7: Chinese reasoning model, strong on structured analysis
        "quant": 0.85, "code": 0.78, "research": 0.82, "market": 0.75,
        "strategy": 0.82, "meta": 0.75, "creative": 0.68, "factual": 0.78,
        "risk": 0.80,
    },
    "groq-llama-3.3-70b": {
        # Llama-3.3-70B on Groq: fast factual recall, less depth
        "quant": 0.72, "code": 0.78, "research": 0.78, "market": 0.75,
        "strategy": 0.72, "meta": 0.68, "creative": 0.72, "factual": 0.88,
        "risk": 0.72,
    },
    "github-gpt-4.1": {
        # GPT-4.1: balanced strong, excellent code reviewer, careful reasoning
        "quant": 0.85, "code": 0.92, "research": 0.88, "market": 0.82,
        "strategy": 0.85, "meta": 0.88, "creative": 0.82, "factual": 0.85,
        "risk": 0.88,
    },
    "github-llama-405b": {
        # Llama-3.1-405B: deepest synthesis, long-form, breadth
        "quant": 0.85, "code": 0.82, "research": 0.92, "market": 0.85,
        "strategy": 0.88, "meta": 0.82, "creative": 0.85, "factual": 0.85,
        "risk": 0.82,
    },
    "github-deepseek-v3": {
        # DeepSeek-V3: math + code specialist
        "quant": 0.90, "code": 0.92, "research": 0.78, "market": 0.72,
        "strategy": 0.78, "meta": 0.75, "creative": 0.68, "factual": 0.78,
        "risk": 0.78,
    },
    "mistral-large": {
        # Mistral Large: european perspective, regulatory awareness, structured
        "quant": 0.78, "code": 0.82, "research": 0.82, "market": 0.85,
        "strategy": 0.82, "meta": 0.78, "creative": 0.78, "factual": 0.82,
        "risk": 0.88,  # regulatory/risk specialty
    },
    "nvidia-nemotron-49b": {
        # Nemotron-49B: instruction-tuned reasoner, careful long-form
        "quant": 0.82, "code": 0.78, "research": 0.85, "market": 0.75,
        "strategy": 0.82, "meta": 0.82, "creative": 0.72, "factual": 0.82,
        "risk": 0.80,
    },
    "sambanova-llama-3.3-70b": {
        # SambaNova Llama-3.3-70B: same model as Groq, different infra
        "quant": 0.72, "code": 0.78, "research": 0.78, "market": 0.75,
        "strategy": 0.72, "meta": 0.68, "creative": 0.72, "factual": 0.85,
        "risk": 0.72,
    },
}


# Domain-specific emphasis prompts appended to each voter's system prompt
DOMAIN_EMPHASIS: dict[str, str] = {
    "quant": "Emphasize mathematical rigor, specific formulas, citations to seminal papers (Almgren-Chriss, Avellaneda-Stoikov, Kyle, BJN), and concrete numerical examples.",
    "code": "Emphasize concrete code (Python with type hints, real libraries, error handling). Include working snippets, not pseudocode. Cite specific libraries by name with versions where relevant.",
    "research": "Emphasize academic rigor — cite papers by author + year, discuss methodology critically, acknowledge contested findings, distinguish your inferences from cited claims.",
    "market": "Emphasize current market microstructure realities — actual venues (HL, Binance, OKX), real fee schedules, real liquidity. Use specific dollar amounts and bps, not abstractions.",
    "strategy": "Emphasize trade-offs explicitly — for each recommendation list the failure mode. Distinguish what works in trending vs ranging regimes. Concrete entry/exit/sizing rules.",
    "meta": "Emphasize the system-level perspective — orchestration patterns, feedback loops, failure recovery, what to monitor. Avoid getting trapped in any single layer's details.",
    "creative": "Emphasize originality and synthesis from disparate fields. Find non-obvious analogies. Steelman unconventional approaches before evaluating them.",
    "factual": "Emphasize precision — specific names, dates, version numbers, exact quotations. If uncertain about a fact, mark it explicitly as inference.",
    "risk": "Emphasize what could go wrong. List failure modes by probability × impact. Include circuit breakers, kill switches, and detection latencies. Be paranoid by default.",
}


GENERALIST_EMPHASIS = (
    "Provide BREADTH — cover angles the specialists might miss. "
    "Connect the question to related domains where useful. "
    "Your role here is the well-read generalist, not the deepest expert."
)


def get_voter_prompt_addition(voter_name: str, domain: str,
                              strength_threshold: float = 0.82) -> str:
    """Return domain-specific guidance to append to a voter's system prompt.

    If voter is strong in the classified domain (score >= threshold) → emphasize that domain.
    If voter is weak in the domain → give them a generalist breadth task.
    This means EVERY voter still fires; their roles are differentiated.
    """
    profile = VOTER_PROFILES.get(voter_name, {})
    score = profile.get(domain, 0.5)
    if score >= strength_threshold:
        return DOMAIN_EMPHASIS.get(domain, "") + f"\n\n[This question is in your strongest domain — score {score:.2f}. Lead with depth.]"
    else:
        return GENERALIST_EMPHASIS + f"\n\n[Domain '{domain}' is not your strongest area — score {score:.2f}. Contribute breadth instead.]"


# ─────────────────── Domain classifier ───────────────────
DOMAIN_CLASSIFIER_SYSTEM = """You classify questions into ONE primary domain.

Domains:
- quant: mathematical finance, derivatives, risk modeling, stochastic calc
- code: software engineering, implementation, debugging, architecture
- research: academic synthesis, literature review, theoretical analysis
- market: live markets, current trading conditions, venues, microstructure
- strategy: trading strategy design, alpha generation, execution rules
- meta: system design, orchestration, AI/ML pipeline, infrastructure
- creative: writing, ideation, brainstorming, non-technical creative
- factual: lookup, current events, definitions, what-is questions
- risk: risk management, hedging, sizing, circuit breakers, audit

Output ONLY this JSON, no preamble:
{"domain": "<one of above>", "confidence": 0.0-1.0, "secondary": "<optional second domain or null>"}
"""


async def classify_domain(query: str, http_client) -> dict:
    """Classify query domain using fastest model. Falls back to 'research' on failure."""
    import os
    api_key = os.environ.get("CEREBRAS_API_KEY", "")
    if not api_key:
        return {"domain": "research", "confidence": 0.3, "secondary": None, "_fallback": "no_api_key"}
    try:
        r = await http_client.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "gpt-oss-120b",   # fastest cerebras model
                "messages": [
                    {"role": "system", "content": DOMAIN_CLASSIFIER_SYSTEM},
                    {"role": "user", "content": query},
                ],
                "max_tokens": 100,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=10.0,
        )
        if r.status_code != 200:
            return {"domain": "research", "confidence": 0.3, "secondary": None,
                    "_fallback": f"http_{r.status_code}"}
        data = r.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        import json as _json
        parsed = _json.loads(content)
        # Validate
        valid_domains = set(DOMAIN_EMPHASIS.keys())
        d = parsed.get("domain", "research")
        if d not in valid_domains:
            d = "research"
        return {
            "domain": d,
            "confidence": float(parsed.get("confidence", 0.5)),
            "secondary": parsed.get("secondary"),
        }
    except Exception as e:
        return {"domain": "research", "confidence": 0.3, "secondary": None,
                "_fallback": str(e)[:100]}
