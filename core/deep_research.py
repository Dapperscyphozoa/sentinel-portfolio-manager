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
    # max_tokens reduced from 8000 to 4000 to fit 512MB Render starter.
    # 4000 tokens ≈ 3000 words, plenty for a research voter.
    ("cerebras-qwen3-235b", "qwen-3-235b-a22b-instruct-2507",
     "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", 4000),
    ("cerebras-gpt-oss-120b", "gpt-oss-120b",
     "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", 4000),
    ("cerebras-glm-4.7", "zai-glm-4.7",
     "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", 4000),
    ("groq-llama-3.3-70b", "llama-3.3-70b-versatile",
     "https://api.groq.com/openai/v1", "GROQ_API_KEY", 4000),
    ("github-gpt-4.1", "openai/gpt-4.1",
     "https://models.github.ai/inference", "GITHUB_MODELS_TOKEN", 4000),
    ("github-llama-405b", "meta/Meta-Llama-3.1-405B-Instruct",
     "https://models.github.ai/inference", "GITHUB_MODELS_TOKEN", 4000),
    ("github-deepseek-v3", "deepseek/DeepSeek-V3-0324",
     "https://models.github.ai/inference", "GITHUB_MODELS_TOKEN", 4000),
    ("mistral-large", "mistral-large-latest",
     "https://api.mistral.ai/v1", "MISTRAL_API_KEY", 4000),
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


async def _call_provider(client: httpx.AsyncClient, prov_tuple, system: str, user: str,
                          temperature: float = 0.65) -> dict:
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
        "temperature": temperature,
    }
    start = time.time()
    try:
        r = await client.post(f"{base_url}/chat/completions",
                              headers=headers, json=body, timeout=150.0)
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


# ─────────────────── Adversarial critique prompt ───────────────────
CRITIQUE_SYSTEM = """You are a HOSTILE peer reviewer at a top-tier quant fund.
The text below is a synthesis from 10 LLMs answering the user's question.
Your job is to find what's WRONG with it. Be brutal. Be specific.

Identify exactly 3-5 of:
- OMISSIONS: critical material that should be there but isn't
- OVERCONFIDENCE: claims stated as fact that are actually contested
- ERRORS: outright wrong statements (be specific, cite what's wrong)
- BLINDSPOTS: framings the synthesis takes for granted that a strong reviewer would challenge
- BURIED INSIGHTS: things that are mentioned but should be central

Output format:
## Critique
1. [TYPE] specific issue with exact quote or reference to which section
2. [TYPE] ...
3. [TYPE] ...

## What's missing entirely
Brief enumeration of topics the synthesis SHOULD have covered but didn't.

## One concrete improvement
The single most impactful change you'd demand before approving this for publication.

Do NOT be polite. Do NOT hedge. Do NOT compliment. Your value is in dissent."""


async def run_deep_research(query: str, providers_subset: Optional[list] = None,
                            entry_id: Optional[str] = None,
                            enable_critique: bool = True,
                            progress_cb=None) -> dict:
    """Two-stage deep research:

    STAGE 1 — Skill-conditioned routing:
      Classify query domain (3s, fastest model)
      Fire ALL 10 voters in parallel with PER-VOTER prompt additions:
        Strong-in-domain voters → emphasize their specialty
        Weak-in-domain voters   → asked to provide breadth/generalist coverage
      Diversity preserved: every voter still fires.
      Synthesize first-pass.

    STAGE 2 — Adversarial critique (closed-loop refinement):
      Send first-pass synthesis to 3 critique voters in parallel.
      Each critic produces hostile peer-review.
      Final refinement integrates synthesis + critiques.

    progress_cb: optional callable(phase: str, extra: dict) for progress updates.
    """
    import hashlib
    from core.voter_skills import (classify_domain, get_voter_prompt_addition,
                                    VOTER_PROFILES)
    from core import skill_ledger

    def _pcb(phase: str, **extra):
        if progress_cb:
            try:
                progress_cb(phase, extra or {})
            except Exception:
                pass

    start = time.time()
    entry_id = entry_id or f"deep_{int(time.time())}"
    query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]

    available = [p for p in PROVIDERS
                 if (providers_subset is None or p[0] in providers_subset)
                 and os.environ.get(p[3], "")]
    log.info("[deep:%s] firing %d voters", entry_id, len(available))
    _pcb("starting", providers_planned=len(available))

    async with httpx.AsyncClient() as client:
        # ─── STAGE 0: Domain classification (~3s) ───
        _pcb("classifying")
        dom_start = time.time()
        domain_info = await classify_domain(query, client)
        dom_elapsed = time.time() - dom_start
        domain = domain_info["domain"]
        log.info("[deep:%s] domain=%s conf=%.2f in %.1fs",
                 entry_id, domain, domain_info["confidence"], dom_elapsed)
        _pcb("voters_firing", domain=domain, classify_s=round(dom_elapsed, 1))

        # ─── STAGE 1: Fire voters in BATCHES (memory-safe on 512MB plan) ───
        # Holding all 10 raw httpx responses + parsed JSON + Python objects in
        # memory simultaneously pushes us over the 512MB starter limit (OOM).
        # Solution: fire in batches of BATCH_SIZE. Each batch completes, gets
        # trimmed, then next batch starts. Total wall time unchanged because
        # the slowest voter per batch still gates the batch, but peak memory
        # drops ~3-4x.
        import gc as _gc
        BATCH_SIZE = int(os.environ.get("DEEP_VOTER_BATCH", "5"))
        _voters_done = {"count": 0}

        async def call_with_skill(prov):
            voter_name = prov[0]
            skill_addition = get_voter_prompt_addition(voter_name, domain)
            full_system = DEEP_RESEARCH_SYSTEM + "\n\n" + skill_addition
            result = await _call_provider(client, prov, full_system, query)
            _voters_done["count"] += 1
            _pcb("voters_progress",
                 voters_done=_voters_done["count"],
                 voters_total=len(available),
                 last_voter=voter_name,
                 last_ok=result.get("ok", False))
            skill_ledger.record_voter_call(
                entry_id=entry_id, query_hash=query_hash, domain=domain,
                voter_name=voter_name, model_id=prov[1],
                elapsed_s=result.get("elapsed_s", 0),
                word_count=result.get("words", 0),
                ok=result.get("ok", False),
                error_text=result.get("error"),
                is_critique=False,
            )
            result["assigned_domain"] = domain
            result["domain_skill_score"] = VOTER_PROFILES.get(voter_name, {}).get(domain, 0.5)
            result["role"] = "specialist" if result["domain_skill_score"] >= 0.82 else "generalist"
            return result

        voter_phase_start = time.time()
        results: list = []
        for i in range(0, len(available), BATCH_SIZE):
            batch = available[i:i + BATCH_SIZE]
            batch_results = await asyncio.gather(*[call_with_skill(p) for p in batch])
            results.extend(batch_results)
            # Force GC between batches — releases httpx response buffers
            _gc.collect()
        voters_elapsed = time.time() - voter_phase_start
        ok_voters = [r for r in results if r.get("ok") and r.get("content")]
        log.info("[deep:%s] voters done in %.1fs: %d/%d ok (batched %d at a time)",
                 entry_id, voters_elapsed, len(ok_voters), len(available), BATCH_SIZE)
        _pcb("synthesizing_first",
             voters_ok=len(ok_voters),
             voters_total=len(available),
             voters_s=round(voters_elapsed, 1))

        if not ok_voters:
            return {
                "entry_id": entry_id, "domain": domain_info,
                "voters": results, "critiques": [],
                "first_synthesis": "", "refined_synthesis": "ERROR: no voters returned.",
                "timing": {"domain_classify_s": round(dom_elapsed, 1),
                           "voters_s": round(voters_elapsed, 1),
                           "synth1_s": 0, "critique_s": 0, "refine_s": 0,
                           "total_s": round(time.time() - start, 1)},
                "providers_called": len(available), "providers_succeeded": 0,
                "first_words": 0, "refined_words": 0, "critiques_succeeded": 0,
            }

        # ─── STAGE 1b: First synthesis ───
        ok_sorted = sorted(ok_voters, key=lambda r: -r.get("words", 0))[:5]
        synth_input = f"USER QUESTION:\n{query}\n\nDOMAIN: {domain}\n\n"
        for i, v in enumerate(ok_sorted, 1):
            synth_input += f"\n═══ VOTER {i} ({v['model']}, role={v.get('role','?')}, {v.get('words',0)}w) ═══\n{v['content']}\n"
        synth_input += "\n═══ END OF VOTERS ═══\n\nSynthesize the BEST and MOST COMPREHENSIVE single response covering all important content. Be longer and deeper than any individual voter. Use the role tags — specialists got domain-specific prompts, generalists got breadth — so integrate accordingly."

        synth_prov = ("cerebras-qwen3-235b", "qwen-3-235b-a22b-instruct-2507",
                      "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", 8000)
        synth_start = time.time()
        synth_result = await _call_provider(client, synth_prov, SYNTHESIZER_SYSTEM,
                                            synth_input, temperature=0.55)
        synth_elapsed = time.time() - synth_start
        first_synthesis = synth_result.get("content", "") if synth_result.get("ok") else ""
        if not first_synthesis:
            first_synthesis = ok_sorted[0]["content"]
        log.info("[deep:%s] synth1 done in %.1fs (%d words)",
                 entry_id, synth_elapsed, len(first_synthesis.split()))

        # ─── STAGE 2: Adversarial critique (3 critics in parallel) ───
        critiques = []
        critique_elapsed = 0.0
        refined_synthesis = first_synthesis
        refine_elapsed = 0.0

        if enable_critique and first_synthesis:
            _pcb("critiquing", first_words=len(first_synthesis.split()))
            # Pick 3 strongest critics (different strengths than first synth)
            # gpt-4.1 for code/risk; mistral-large for risk/european perspective;
            # nemotron-49b for careful instruction following.
            critic_names = ["github-gpt-4.1", "mistral-large", "nvidia-nemotron-49b"]
            critic_provs = [p for p in available if p[0] in critic_names]

            critique_input = (
                f"ORIGINAL QUESTION:\n{query}\n\n"
                f"DOMAIN: {domain}\n\n"
                f"SYNTHESIS TO CRITIQUE:\n{first_synthesis}\n\n"
                f"Now critique this synthesis ruthlessly per your instructions."
            )

            async def call_critic(prov):
                result = await _call_provider(client, prov, CRITIQUE_SYSTEM,
                                              critique_input, temperature=0.4)
                skill_ledger.record_voter_call(
                    entry_id=entry_id, query_hash=query_hash, domain=domain,
                    voter_name=prov[0], model_id=prov[1],
                    elapsed_s=result.get("elapsed_s", 0),
                    word_count=result.get("words", 0),
                    ok=result.get("ok", False),
                    error_text=result.get("error"),
                    is_critique=True,
                )
                result["assigned_domain"] = domain
                result["role"] = "critic"
                return result

            crit_start = time.time()
            critiques = await asyncio.gather(*[call_critic(p) for p in critic_provs])
            critique_elapsed = time.time() - crit_start
            _gc.collect()  # release critic response buffers
            ok_critiques = [c for c in critiques if c.get("ok") and c.get("content")]
            log.info("[deep:%s] critiques done in %.1fs: %d/%d ok",
                     entry_id, critique_elapsed, len(ok_critiques), len(critic_provs))

            # ─── STAGE 2b: Refinement synthesis ───
            if ok_critiques:
                _pcb("refining", critiques_ok=len(ok_critiques))
                refine_input = (
                    f"ORIGINAL QUESTION:\n{query}\n\nDOMAIN: {domain}\n\n"
                    f"FIRST-PASS SYNTHESIS:\n{first_synthesis}\n\n"
                    f"═══ HOSTILE CRITIQUES ═══\n"
                )
                for i, c in enumerate(ok_critiques, 1):
                    refine_input += f"\nCRITIC {i} ({c['model']}):\n{c['content']}\n"
                refine_input += (
                    "\n═══ END OF CRITIQUES ═══\n\n"
                    "Produce the FINAL refined synthesis. Address every legitimate "
                    "critique. Add the missing content. Fix the overconfident claims. "
                    "Steelman dissenting framings where they have merit. "
                    "Keep what was strong. Be MORE comprehensive than the first pass, "
                    "not less. Aim for 2500+ words for research questions. "
                    "Note explicitly where you accepted/rejected each critique."
                )

                REFINE_SYSTEM = SYNTHESIZER_SYSTEM + (
                    "\n\nYou are doing a SECOND-PASS REFINEMENT after hostile peer review. "
                    "Your job is integration, not summarization. The result must be a single "
                    "coherent document that is BETTER than the first pass on every axis."
                )

                refine_start = time.time()
                refine_result = await _call_provider(
                    client, synth_prov, REFINE_SYSTEM, refine_input, temperature=0.5,
                )
                refine_elapsed = time.time() - refine_start
                if refine_result.get("ok") and refine_result.get("content"):
                    refined_synthesis = refine_result["content"]
                log.info("[deep:%s] refine done in %.1fs (%d words)",
                         entry_id, refine_elapsed, len(refined_synthesis.split()))

    # Memory savings: the stored result dict sits in _DEEP_JOBS for 10 minutes.
    # Don't keep full voter/critique content there — the synthesis already
    # contains the merged information. Keep first 1500 chars per voter for
    # the inspection panel; drop the rest.
    _MAX_VOTER_SNIPPET = 1500
    for v in results:
        if v.get("content") and len(v["content"]) > _MAX_VOTER_SNIPPET:
            v["content"] = v["content"][:_MAX_VOTER_SNIPPET] + "…[trimmed]"
    for c in critiques:
        if c.get("content") and len(c["content"]) > _MAX_VOTER_SNIPPET:
            c["content"] = c["content"][:_MAX_VOTER_SNIPPET] + "…[trimmed]"
    _gc.collect()

    total = time.time() - start
    return {
        "entry_id": entry_id,
        "domain": domain_info,
        "voters": results,
        "critiques": critiques,
        "first_synthesis": first_synthesis,
        "refined_synthesis": refined_synthesis,
        "synth_model": synth_prov[1],
        "timing": {
            "domain_classify_s": round(dom_elapsed, 1),
            "voters_s": round(voters_elapsed, 1),
            "synth1_s": round(synth_elapsed, 1),
            "critique_s": round(critique_elapsed, 1),
            "refine_s": round(refine_elapsed, 1),
            "total_s": round(total, 1),
        },
        "providers_called": len(available),
        "providers_succeeded": len(ok_voters),
        "first_words": len(first_synthesis.split()),
        "refined_words": len(refined_synthesis.split()),
        "critiques_succeeded": sum(1 for c in critiques if c.get("ok")),
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
