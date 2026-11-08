"""
Optional claim proposer for meetings drafts.

Does NOT publish, review, or write runs by itself.
Returns candidate claims that still must pass evidence_errors
(exact quote offsets) and human review_run + publish.

If LLM_API_KEY / XAI_API_KEY is set, uses an OpenAI-compatible chat endpoint.
Otherwise falls back to a deterministic heuristic.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any


PROPOSER_VERSION = "meetings-propose-v1"
DEFAULT_MODEL = os.environ.get("LLM_MODEL") or "grok-4-fast"
DEFAULT_BASE = (os.environ.get("LLM_BASE_URL") or "https://api.x.ai/v1").rstrip("/")


def _api_key() -> str | None:
    return os.environ.get("LLM_API_KEY") or os.environ.get("XAI_API_KEY")


def propose_claims(
    entries: list[dict],
    *,
    use_llm: bool = True,
    max_claims: int = 20,
) -> dict[str, Any]:
    """
    entries: batch entries with at least index, text, status.
    Returns {claims, model_version, prompt_version, extractor, warnings}.
    """
    accepted = [
        e for e in entries
        if e.get("status") in (None, "accepted", "duplicate") and (e.get("text") or "").strip()
    ]
    if not accepted:
        return {
            "claims": [],
            "model_version": None,
            "prompt_version": PROPOSER_VERSION,
            "extractor": "none",
            "warnings": ["No accepted text entries to analyse"],
        }

    if use_llm and _api_key():
        try:
            return _propose_llm(accepted, max_claims=max_claims)
        except Exception as exc:  # noqa: BLE001 — always degrade, never break intake
            heuristic = _propose_heuristic(accepted, max_claims=max_claims)
            heuristic["warnings"] = list(heuristic.get("warnings") or []) + [
                f"LLM unavailable ({exc}); used heuristic"
            ]
            return heuristic

    return _propose_heuristic(accepted, max_claims=max_claims)


def _find_quote(text: str, needle: str) -> int | None:
    """Return start offset of needle in text, or None."""
    if not needle:
        return None
    idx = text.find(needle)
    if idx >= 0:
        return idx
    # try stripped / collapsed spaces
    compact = re.sub(r"\s+", " ", needle.strip())
    if compact and compact in text:
        return text.find(compact)
    return None


def _propose_heuristic(entries: list[dict], *, max_claims: int) -> dict[str, Any]:
    keywords = (
        "нужно", "сделать", "todo", "action", "поручение", "задача",
        "должен", "должна", "сделаю", "возьму", "подготовить", "отправить",
        "согласовать", "срок", "дедлайн", "к пятниц", "к сред",
    )
    claims: list[dict] = []
    warnings: list[str] = []

    for entry in entries:
        index = int(entry.get("index", 0))
        text = entry["text"]
        for line in text.splitlines():
            line = line.strip()
            if not line or len(line) > 500:
                continue
            low = line.casefold()
            if not any(kw in low for kw in keywords):
                continue
            start = text.find(line)
            if start < 0:
                continue
            # crude subject/value split
            subject = "поручение"
            predicate = "действие"
            value = line
            if ":" in line:
                left, right = line.split(":", 1)
                if left.strip():
                    subject = left.strip()[:200]
                    value = right.strip()[:200] or line
            claims.append({
                "source_index": index,
                "start": start,
                "quote": line[:2000],
                "text": line[:2000],
                "subject": subject[:200],
                "predicate": predicate[:200],
                "value": value[:200],
            })
            if len(claims) >= max_claims:
                break
        if len(claims) >= max_claims:
            break

    if not claims:
        warnings.append("Heuristic found no action-like lines; submit claims manually")

    return {
        "claims": claims[:max_claims],
        "model_version": None,
        "prompt_version": PROPOSER_VERSION + "+heuristic",
        "extractor": "heuristic",
        "warnings": warnings,
    }


def _propose_llm(entries: list[dict], *, max_claims: int) -> dict[str, Any]:
    import httpx

    # Build a compact, indexed transcript for the model
    blocks = []
    for entry in entries:
        blocks.append(f"[source_index={entry.get('index', 0)}]\n{entry['text'][:8000]}")
    joined = "\n\n".join(blocks)[:14000]

    system = (
        "Ты готовишь черновик claims для системы проверяемой публикации встреч. "
        "Верни ТОЛЬКО JSON-объект: {\"claims\":[...]}. "
        "Каждый claim: source_index (int), quote (точная подстрока исходника), "
        "text (краткая формулировка), subject, predicate, value — все строки. "
        "quote ОБЯЗАН быть дословной подстрокой соответствующего source_index. "
        "Не выдумывай факты. Максимум "
        f"{max_claims} claims. Язык — как в исходнике."
    )
    user = "Исходники:\n" + joined

    payload = {
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(f"{DEFAULT_BASE}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    text = content.strip()
    if text.startswith("```"):
        text = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("```"))
    data = json.loads(text)
    raw_claims = data.get("claims") or []

    # Map by source_index for quote verification
    by_index = {int(e.get("index", 0)): e["text"] for e in entries}
    claims: list[dict] = []
    warnings: list[str] = []

    for raw in raw_claims:
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw["source_index"])
        except (KeyError, TypeError, ValueError):
            warnings.append("Dropped claim with invalid source_index")
            continue
        src = by_index.get(index)
        if src is None:
            warnings.append(f"Dropped claim: unknown source_index {index}")
            continue
        quote = (raw.get("quote") or "").strip()
        start = _find_quote(src, quote)
        if start is None:
            warnings.append("Dropped claim: quote not found in source text")
            continue
        # force exact quote from source so evidence_errors will pass
        exact = src[start:start + len(quote)]
        claims.append({
            "source_index": index,
            "start": start,
            "quote": exact[:2000],
            "text": (raw.get("text") or exact)[:2000],
            "subject": str(raw.get("subject") or "факт")[:200],
            "predicate": str(raw.get("predicate") or "значение")[:200],
            "value": str(raw.get("value") or exact)[:200],
        })
        if len(claims) >= max_claims:
            break

    return {
        "claims": claims,
        "model_version": DEFAULT_MODEL,
        "prompt_version": PROPOSER_VERSION,
        "extractor": "llm",
        "warnings": warnings,
    }
