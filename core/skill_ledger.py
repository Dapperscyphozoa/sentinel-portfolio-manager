"""Per-voter rating ledger.

Tracks how well each voter contributes per (domain, question_type).
Updated by user ratings + automated post-hoc analysis.

The skill profiles in voter_skills.py are HEURISTIC PRIORS.
This ledger lets the system LEARN from real outcomes over time.

After ~100 rated queries, the orchestrator should weight voters by their
domain-specific contribution score, not the static prior.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Optional

log = logging.getLogger("skill_ledger")


DEFAULT_DB = os.environ.get("SKILL_LEDGER_DB", "/var/data/skill_ledger.sqlite")


def _conn(path: Optional[str] = None) -> sqlite3.Connection:
    p = path or DEFAULT_DB
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=10000")
    return c


def init_db(path: Optional[str] = None) -> None:
    c = _conn(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS voter_calls (
        ts INTEGER NOT NULL,
        entry_id TEXT NOT NULL,
        query_hash TEXT NOT NULL,
        domain TEXT NOT NULL,
        voter_name TEXT NOT NULL,
        model_id TEXT NOT NULL,
        elapsed_s REAL,
        word_count INTEGER,
        ok INTEGER,
        error_text TEXT,
        is_critique INTEGER DEFAULT 0,
        is_synth_input INTEGER DEFAULT 0,
        PRIMARY KEY (entry_id, voter_name, is_critique)
    );
    CREATE INDEX IF NOT EXISTS idx_voter_domain ON voter_calls (voter_name, domain);
    CREATE INDEX IF NOT EXISTS idx_entry ON voter_calls (entry_id);

    CREATE TABLE IF NOT EXISTS query_ratings (
        entry_id TEXT PRIMARY KEY,
        query_hash TEXT NOT NULL,
        domain TEXT NOT NULL,
        rating INTEGER,
        rated_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS voter_contributions (
        entry_id TEXT NOT NULL,
        voter_name TEXT NOT NULL,
        domain TEXT NOT NULL,
        contribution_score REAL,
        used_in_synthesis INTEGER DEFAULT 0,
        is_critique INTEGER DEFAULT 0,
        notes TEXT,
        PRIMARY KEY (entry_id, voter_name, is_critique)
    );
    """)
    c.commit()
    c.close()


def record_voter_call(entry_id: str, query_hash: str, domain: str,
                      voter_name: str, model_id: str,
                      elapsed_s: float, word_count: int, ok: bool,
                      error_text: Optional[str] = None,
                      is_critique: bool = False) -> None:
    try:
        c = _conn()
        c.execute(
            """INSERT OR REPLACE INTO voter_calls
               (ts, entry_id, query_hash, domain, voter_name, model_id,
                elapsed_s, word_count, ok, error_text, is_critique)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (int(time.time() * 1000), entry_id, query_hash, domain,
             voter_name, model_id, elapsed_s, word_count, int(ok),
             error_text or "", int(is_critique)),
        )
        c.commit()
        c.close()
    except Exception as e:
        log.warning("record_voter_call failed: %s", e)


def record_user_rating(entry_id: str, query_hash: str, domain: str, rating: int) -> None:
    try:
        c = _conn()
        c.execute(
            """INSERT OR REPLACE INTO query_ratings
               (entry_id, query_hash, domain, rating, rated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entry_id, query_hash, domain, rating, int(time.time() * 1000)),
        )
        c.commit()
        c.close()
    except Exception as e:
        log.warning("record_user_rating failed: %s", e)


def voter_domain_stats(voter_name: str, domain: Optional[str] = None,
                       since_ts: Optional[int] = None) -> dict:
    """Return aggregated stats for a voter (optionally filtered by domain + time)."""
    c = _conn()
    where = "WHERE voter_name = ?"
    args: list = [voter_name]
    if domain:
        where += " AND domain = ?"
        args.append(domain)
    if since_ts:
        where += " AND ts >= ?"
        args.append(since_ts)
    rows = c.execute(f"""
        SELECT
          COUNT(*) AS n,
          SUM(ok) AS ok_count,
          AVG(elapsed_s) AS avg_elapsed,
          AVG(word_count) AS avg_words
        FROM voter_calls {where}
    """, args).fetchone()
    # Rating contribution: avg rating of queries this voter contributed to
    rating_row = c.execute(f"""
        SELECT AVG(qr.rating) AS avg_rating, COUNT(qr.rating) AS n_rated
        FROM voter_calls vc
        JOIN query_ratings qr ON qr.entry_id = vc.entry_id
        {where.replace('voter_name', 'vc.voter_name').replace('domain', 'vc.domain')
              .replace('ts', 'vc.ts')}
          AND vc.ok = 1
    """, args).fetchone()
    c.close()
    return {
        "voter_name": voter_name,
        "domain": domain or "all",
        "n_calls": rows["n"] if rows else 0,
        "ok_rate": (rows["ok_count"] / rows["n"]) if rows and rows["n"] else 0,
        "avg_elapsed_s": rows["avg_elapsed"] if rows else None,
        "avg_words": rows["avg_words"] if rows else None,
        "avg_rating": rating_row["avg_rating"] if rating_row else None,
        "n_rated": rating_row["n_rated"] if rating_row else 0,
    }


def learned_weight(voter_name: str, domain: str,
                   min_samples: int = 5,
                   prior_weight: float = 0.5) -> tuple[float, str]:
    """Return a learned weight for (voter, domain).

    Returns (weight, basis) where basis is "learned" if enough samples,
    else "prior" indicating use the heuristic profile.

    Weight is a Bayesian-shrunk avg rating: with few samples, leans toward prior_weight=0.5.
    With many samples, leans toward observed avg rating.
    """
    stats = voter_domain_stats(voter_name, domain)
    n = stats["n_rated"] or 0
    if n < min_samples:
        return (prior_weight, "prior")
    obs_rating = (stats["avg_rating"] or 0) / 5.0  # normalize 1-5 to 0.2-1.0
    # Bayesian shrinkage: weight = (alpha + obs_n*obs) / (alpha + obs_n)
    # with prior = 0.5 (neutral) and alpha = min_samples (acts as pseudo-count)
    alpha = float(min_samples)
    weight = (alpha * prior_weight + n * obs_rating) / (alpha + n)
    return (round(weight, 3), "learned")


def all_voter_summary() -> list[dict]:
    """Get summary across all voters and domains for /api/voter_skills inspection."""
    c = _conn()
    rows = c.execute("""
        SELECT
          voter_name, domain,
          COUNT(*) AS n_calls,
          SUM(ok) AS ok_count,
          AVG(elapsed_s) AS avg_elapsed,
          AVG(word_count) AS avg_words
        FROM voter_calls
        GROUP BY voter_name, domain
        ORDER BY voter_name, domain
    """).fetchall()
    out = [dict(r) for r in rows]
    c.close()
    return out


# Initialize DB on import
try:
    init_db()
except Exception as e:
    log.warning("skill_ledger init failed: %s", e)
