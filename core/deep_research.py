"""Deep research — fires top-tier models in parallel with NO word caps.

Bypasses sentinel /ask's 80-word truncation. Designed for MM-quant-depth
research, technical writeups, comprehensive analysis.

Providers + models (OpenAI-compatible APIs):
  - cerebras: qwen-3-235b-a22b-instruct       (strongest reasoning, 8k output)
  - cerebras: qwen-3-coder-480b               (best for code-heavy responses)
  - groq:     llama-3.3-70b-versatile         (fast, factual)
  - github:   openai/gpt-5                    (top-tier general)
  - github:   meta/Meta-Llama-3.1-405B        (largest open weight)
  - github:   deepseek/DeepSeek-V3-0324       (reasoning)
  - mistral:  mistral-large-latest            (european perspective)
  - nvidia:   llama-3.3-nemotron-super-49b    (instruction-tuned)
  - sambanova: Meta-Llama-3.3-70B-Instruct    (speed)

Returns: each model's full answer + synthesis (one large coherent response).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import httpx

log = logging.getLogger("deep_research")


# ─────────────────── Provider catalog ───────────────────
PROVIDERS = [
    # (provider_name, model_id, base_url, env_key, max_tokens)
    ("cerebras-qwen3-235b", "qwen-3-235b-a22b-instruct",
     "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", 8000),
    ("cerebras-qwen3-coder", "qwen-3-coder-480b",
     "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", 8000),
    ("groq-llama-3.3-70b", "llama-3.3-70b-versatile",
     "https://api.groq.com/openai/v1", "GROQ_API_KEY", 8000),
    ("github-gpt-5", "openai/gpt-5",
     "https://models.github.ai/inference", "GITHUB_MODELS_TOKEN", 8000),
    ("github-llama-405b", "meta/Meta-Llama-3.1-405B-Instruct",
     "https://models.github.ai/inference", "GITHUB_MODELS_TOKEN", 4000),
    ("github-deepseek-v3", "deepseek/DeepSeek-V3-0324",
     "https://models.github.ai/inference", "GITHUB_MODELS_TOKEN", 4000),
    ("mistral-large", "mistral-large-latest",
     "https://api.mistral.ai/v1", "MISTRAL_API_KEY", 8000),
    ("nvidia-nemotron-49b", "nvidia/llama-3.3-nemotron-super-49b-v1",
     "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", 4000),
    ("sambanova-llama-3.3-70b", "Meta-Llama-3.3-70B-Instruct",
     "https://api.sambanova.ai/v1", "SAMBANOVA_API_KEY", 4000),
]


# ─────────────────── Prompt templates ───────────────────
DEEP_RESEARCH_SYSTEM = """You are a market-maker quant trader with 15+ years at Jane Street, Citadel, and Two Sigma. You teach institutional-grade material — the depth Bloomberg Intelligence, GS strats, and DE Shaw recruits demand.

Your responses MUST:
1. Be COMPREHENSIVE — minimum 1500 words for research questions
2. Use CONCRETE numbers, specific tools, real implementations, named papers
3. Structure with clear sections (use markdown ## headers)
4. Include actual CODE where relevant (Python, SQL, KDB+/q where appropriate)
5. Cover: market microstructure, alpha, risk, execution, data engineering
6. NEVER hedge or be vague — speak with the conviction of someone who has run desks
7. Cite specific papers (Almgren-Chriss, Avellaneda-Stoikov, Kyle, BJN, etc.) where relevant
8. Distinguish between retail-naive, sophisticated-retail, and institutional approaches
9. Note where common assumptions break (e.g., GBM in crypto)
10. End with a specific actionable plan or curriculum, not generic advice

DO NOT write generic introductory material. DO NOT pad with platitudes. Assume the reader knows what RSI is, what Sharpe is, what an LSTM is. Skip to the depth."""


SYNTHESIZER_SYSTEM = """You are the lead instructor synthesizing research from 9 expert voters into one authoritative response.

Your synthesis MUST:
1. Be MORE comprehensive than any single voter (combine the best of each)
2. Identify and KEEP every concrete number, citation, code snippet from any voter
3. Resolve disagreements with explicit reasoning (cite which voter said what)
4. Present a single coherent curriculum/answer, not a list of opinions
5. Be structured with markdown ## headers
6. Output minimum 2000 words for research questions
7. End with concrete next-steps section

DO NOT just summarize. DO NOT remove specifics. ADD value through synthesis."""


async def _call_provider(client: httpx.AsyncClient, prov_tuple, system: str, user: str) -> dict:
    name, model, base_url, env_key, max_tokens = prov_tuple
    api_key = os.environ.get(env_key, "")
    if not api_key:
        return {"provider": name, "model": model, "ok": False, "error": f"no_{env_key}"}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0.65,
    }
    start = time.time()
    try:
        r = await client.post(f"{base_url}/chat/completions",
                              headers=headers, json=body, timeout=90.0)
        elapsed = time.time() - start
        if r.status_code != 200:
            return {"provider": name, "model": model, "ok": False,
                    "error": f"http_{r.status_code}",
                    "detail": r.text[:300], "elapsed_s": round(elapsed, 1)}
        data = r.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {
            "provider": name, "model": model, "ok": True,
            "content": content, "elapsed_s": round(elapsed, 1),
            "tokens": (data.get("usage") or {}).get("total_tokens", 0),
            "words": len(content.split()),
        }
    except Exception as e:
        return {"provider": name, "model": model, "ok": False,
                "error": str(e)[:200], "elapsed_s": round(time.time() - start, 1)}


async def run_deep_research(query: str, providers_subset: Optional[list] = None) -> dict:
    """Fire all providers in parallel. Return per-voter responses + synthesis.

    Args:
        query: user's research question
        providers_subset: list of provider names to use (None = all available)

    Returns:
        {
          "voters": [{provider, model, ok, content, elapsed_s, words}, ...],
          "synthesis": str,  # final combined answer
          "synth_elapsed_s": float,
          "total_elapsed_s": float,
          "providers_called": int,
          "providers_succeeded": int,
        }
    """
    start = time.time()
    use = [p for p in PROVIDERS
           if (providers_subset is None or p[0] in providers_subset)
           and os.environ.get(p[3], "")]
    log.info("deep_research: firing %d providers in parallel", len(use))

    async with httpx.AsyncClient() as client:
        # Fire all voters in parallel
        results = await asyncio.gather(
            *[_call_provider(client, p, DEEP_RESEARCH_SYSTEM, query) for p in use]
        )
        voters_phase = time.time() - start
        ok_voters = [r for r in results if r.get("ok") and r.get("content")]
        log.info("voters done in %.1fs: %d/%d ok", voters_phase, len(ok_voters), len(use))

        if not ok_voters:
            return {
                "voters": results,
                "synthesis": "ERROR: no voters returned a response.",
                "total_elapsed_s": round(time.time() - start, 1),
                "providers_called": len(use),
                "providers_succeeded": 0,
            }

        # Synthesizer: pass top 5 longest voter responses to the strongest model
        ok_sorted = sorted(ok_voters, key=lambda r: -r.get("words", 0))[:5]
        synth_input = f"USER QUESTION:\n{query}\n\n"
        for i, v in enumerate(ok_sorted, 1):
            synth_input += f"\n═══ VOTER {i} ({v['model']}, {v.get('words',0)} words) ═══\n{v['content']}\n"
        synth_input += "\n═══ END OF VOTERS ═══\n\nNow synthesize the BEST and MOST COMPREHENSIVE single response covering everything important from the voters above. Be longer and deeper than any individual voter."

        # Use cerebras qwen-3-235b as synthesizer (8k output, fast, strong reasoning)
        synth_prov = ("cerebras-qwen3-235b", "qwen-3-235b-a22b-instruct",
                      "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", 8000)
        synth_start = time.time()
        synth_result = await _call_provider(client, synth_prov, SYNTHESIZER_SYSTEM, synth_input)
        synth_elapsed = time.time() - synth_start
        log.info("synthesis done in %.1fs", synth_elapsed)

        synthesis = synth_result.get("content", "")
        if not synthesis or not synth_result.get("ok"):
            # Fallback: use longest voter answer
            synthesis = ok_sorted[0]["content"]
            log.warning("synthesizer failed, falling back to longest voter")

    total = time.time() - start
    return {
        "voters": results,
        "synthesis": synthesis,
        "synth_model": synth_prov[1],
        "synth_elapsed_s": round(synth_elapsed, 1),
        "voters_elapsed_s": round(voters_phase, 1),
        "total_elapsed_s": round(total, 1),
        "providers_called": len(use),
        "providers_succeeded": len(ok_voters),
        "synth_words": len(synthesis.split()),
    }


def is_deep_query(text: str) -> bool:
    """Heuristic: route to deep research if query asks for depth."""
    t = text.lower()
    triggers = [
        "deep research", "deep dive", "deep analysis", "comprehensive",
        "in depth", "in-depth", "effort max", "max effort",
        "teach", "curriculum", "lecture", "syllabus", "course",
        "rival bloomberg", "rivals bloomberg", "institutional",
        "thorough", "detailed analysis", "comprehensive review",
        "full breakdown", "deep breakdown",
    ]
    return any(t.find(trig) >= 0 for trig in triggers)
