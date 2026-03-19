# -*- coding: utf-8 -*-
"""
===================================
名称/代码解析器
===================================

Resolve stock inputs to a canonical code:
- direct code normalization
- local stock-name map + aliases
- pinyin fallback for local Chinese names
- AkShare fallback for A-shares
- Yahoo Finance search fallback for foreign company names
- fuzzy matching as a conservative last resort
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from src.data.stock_mapping import STOCK_NAME_ALIASES, STOCK_NAME_MAP
from src.services.stock_code_utils import is_code_like, normalize_code

logger = logging.getLogger(__name__)

# AkShare result cache: (timestamp, name_to_code_dict)
_akshare_cache: Optional[tuple[float, Dict[str, str]]] = None
_AKSHARE_CACHE_TTL = 3600  # 1 hour

# Yahoo Finance name-search cache: normalized_query -> (timestamp, resolved_code|None)
_yfinance_search_cache: Dict[str, Tuple[float, Optional[str]]] = {}
_YFINANCE_SEARCH_CACHE_TTL = 3600  # 1 hour

# AI name-resolution cache: normalized_query -> (timestamp, resolved_code|None)
_ai_resolution_cache: Dict[str, Tuple[float, Optional[str]]] = {}
_AI_RESOLUTION_CACHE_TTL = 3600  # 1 hour
_AI_DIRECT_CODE_MIN_CONFIDENCE = 0.92

_LOOKUP_CLEAN_RE = re.compile(r"[^0-9A-Z\u4e00-\u9fff]+", re.IGNORECASE)
_COMPANY_SUFFIX_PATTERNS = (
    re.compile(r"\b(class\s+[a-z])\b", re.IGNORECASE),
    re.compile(
        r"\b(group|holdings?|holding|company|co|corporation|corp|incorporated|inc|plc|limited|ltd|adr)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(common\s+stock|ordinary\s+shares?)\b", re.IGNORECASE),
)


def _is_code_like(s: str) -> bool:
    """Backward-compatible wrapper of shared code-like check."""
    return is_code_like(s)


def _normalize_code(raw: str) -> Optional[str]:
    """Backward-compatible wrapper of shared code normalization."""
    return normalize_code(raw)


def _build_reverse_map_no_duplicates(
    code_to_name: Dict[str, str],
) -> Dict[str, str]:
    """
    Build name -> code map. If a name maps to multiple codes (ambiguous), exclude it.
    """
    name_to_codes: Dict[str, Set[str]] = {}
    for code, name in code_to_name.items():
        if not name or not code:
            continue
        cleaned_name = name.strip()
        if not cleaned_name:
            continue
        name_to_codes.setdefault(cleaned_name, set()).add(code.strip().upper())
    return {name: next(iter(codes)) for name, codes in name_to_codes.items() if len(codes) == 1}


def _normalize_lookup_name(value: str) -> str:
    """Normalize a company name for case-insensitive / suffix-insensitive matching."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if not text:
        return ""

    text = text.replace("&", " and ")
    for pattern in _COMPANY_SUFFIX_PATTERNS:
        text = pattern.sub(" ", text)
    text = _LOOKUP_CLEAN_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace(" ", "")


def _build_alias_reverse_maps(
    code_to_name: Dict[str, str],
    code_to_aliases: Dict[str, Iterable[str]],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Build exact and normalized alias reverse maps, excluding ambiguous aliases.
    """
    exact_to_codes: Dict[str, Set[str]] = {}
    normalized_to_codes: Dict[str, Set[str]] = {}

    def register(alias: str, code: str) -> None:
        alias_text = (alias or "").strip()
        normalized_code = (code or "").strip().upper()
        if not alias_text or not normalized_code:
            return

        exact_to_codes.setdefault(alias_text, set()).add(normalized_code)

        normalized_alias = _normalize_lookup_name(alias_text)
        if normalized_alias:
            normalized_to_codes.setdefault(normalized_alias, set()).add(normalized_code)

    for code, name in code_to_name.items():
        register(name, code)

    for code, aliases in code_to_aliases.items():
        for alias in aliases or ():
            register(alias, code)

    exact_reverse = {
        alias: next(iter(codes))
        for alias, codes in exact_to_codes.items()
        if len(codes) == 1
    }
    normalized_reverse = {
        alias: next(iter(codes))
        for alias, codes in normalized_to_codes.items()
        if len(codes) == 1
    }
    return exact_reverse, normalized_reverse


def _collect_local_alias_candidates(raw_input: str) -> Tuple[Set[str], Set[str]]:
    """Collect exact/normalized local alias matches, preserving ambiguity."""
    exact_codes: Set[str] = set()
    normalized_codes: Set[str] = set()
    normalized_input = _normalize_lookup_name(raw_input)

    def consider(alias: str, code: str) -> None:
        alias_text = (alias or "").strip()
        normalized_code = (code or "").strip().upper()
        if not alias_text or not normalized_code:
            return
        if alias_text == raw_input:
            exact_codes.add(normalized_code)
        if normalized_input and _normalize_lookup_name(alias_text) == normalized_input:
            normalized_codes.add(normalized_code)

    for code, name in STOCK_NAME_MAP.items():
        consider(name, code)
    for code, aliases in STOCK_NAME_ALIASES.items():
        for alias in aliases or ():
            consider(alias, code)

    return exact_codes, normalized_codes


def _is_local_ambiguous_input(raw_input: str) -> bool:
    """Skip AI guessing when the local mappings already know the input is ambiguous."""
    exact_codes, normalized_codes = _collect_local_alias_candidates(raw_input)
    if len(exact_codes) > 1:
        return True
    if not exact_codes and len(normalized_codes) > 1:
        return True
    return False


def _get_akshare_name_to_code() -> Optional[Dict[str, str]]:
    """Fetch A-share name->code from AkShare, with cache."""
    global _akshare_cache
    now = time.time()
    if _akshare_cache is not None and (now - _akshare_cache[0]) < _AKSHARE_CACHE_TTL:
        return _akshare_cache[1]
    try:
        import akshare as ak

        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            return None

        code_to_name: Dict[str, str] = {}
        for _, row in df.iterrows():
            code = row.get("code")
            name = row.get("name")
            if code is None or name is None:
                continue
            code_str = str(code).strip()
            if "." in code_str:
                base, suffix = code_str.rsplit(".", 1)
                if suffix.upper() in ("SH", "SZ", "SS") and base.isdigit():
                    code_str = base
            code_to_name[code_str] = str(name).strip()

        result = _build_reverse_map_no_duplicates(code_to_name)
        _akshare_cache = (now, result)
        logger.info("[NameResolver] AkShare cache loaded: %s name->code mappings", len(result))
        return result
    except Exception as exc:
        logger.warning("[NameResolver] AkShare fallback failed: %s", exc)
        return None


def _is_single_char_typo(input_name: str, candidate_name: str) -> bool:
    """Return True when two names only differ by one character position."""
    if not input_name or not candidate_name:
        return False
    if len(input_name) != len(candidate_name):
        return False
    if len(input_name) < 3:
        return False
    diff = sum(1 for a, b in zip(input_name, candidate_name) if a != b)
    return diff == 1


def _contains_ascii_letters(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text or ""))


def _normalize_search_symbol(symbol: str) -> Optional[str]:
    """
    Normalize Yahoo search result symbols to app-friendly codes.
    """
    text = (symbol or "").strip().upper()
    if not text:
        return None

    normalized = _normalize_code(text)
    if normalized:
        return normalized

    if text.endswith(".HK"):
        base = text[:-3].strip()
        if base.isdigit() and 1 <= len(base) <= 5:
            return base.zfill(5)

    if text.endswith(".BJ"):
        base = text[:-3].strip()
        if base.isdigit() and len(base) == 6:
            return base

    if re.match(r"^[A-Z]{1,5}-[A-Z]$", text):
        return text.replace("-", ".")

    return None


def _score_yfinance_quote(query_key: str, quote: Dict[str, object]) -> Tuple[float, Optional[str]]:
    symbol = _normalize_search_symbol(str(quote.get("symbol") or ""))
    if not symbol:
        return 0.0, None

    quote_type = str(quote.get("quoteType") or "").upper()
    if quote_type and quote_type not in {"EQUITY", "ETF"}:
        return 0.0, None

    candidate_names = [
        str(quote.get("shortname") or ""),
        str(quote.get("longname") or ""),
        str(quote.get("displayName") or ""),
    ]
    candidate_keys = {
        normalized
        for normalized in (_normalize_lookup_name(item) for item in candidate_names)
        if normalized
    }
    if not candidate_keys:
        return 0.0, None

    score = 0.0
    if query_key in candidate_keys:
        score = 1.0
    elif len(query_key) >= 4 and any(query_key in key for key in candidate_keys):
        score = 0.9
    else:
        score = max(
            difflib.SequenceMatcher(a=query_key, b=candidate_key).ratio()
            for candidate_key in candidate_keys
        )

    if quote_type == "EQUITY":
        score += 0.05
    return score, symbol


def _search_foreign_name_to_code(query: str) -> Optional[str]:
    """
    Resolve foreign company names via Yahoo Finance search as a last-mile fallback.
    """
    normalized_query = _normalize_lookup_name(query)
    if not normalized_query or not _contains_ascii_letters(query):
        return None

    now = time.time()
    cached = _yfinance_search_cache.get(normalized_query)
    if cached is not None and (now - cached[0]) < _YFINANCE_SEARCH_CACHE_TTL:
        return cached[1]

    resolved: Optional[str] = None
    try:
        import yfinance as yf

        search = yf.Search(
            query=query,
            max_results=10,
            news_count=0,
            lists_count=0,
            include_cb=False,
            include_nav_links=False,
            include_research=False,
            include_cultural_assets=False,
            enable_fuzzy_query=True,
            recommended=10,
            timeout=10,
            raise_errors=False,
        )

        best_score = 0.0
        for quote in getattr(search, "quotes", []) or []:
            score, symbol = _score_yfinance_quote(normalized_query, quote)
            if score > best_score and symbol:
                best_score = score
                resolved = symbol

        if best_score < 0.8:
            resolved = None
        elif resolved:
            logger.info("[NameResolver] Yahoo search resolved %s -> %s", query, resolved)
    except Exception as exc:
        logger.warning("[NameResolver] Yahoo search fallback failed for %s: %s", query, exc)
        resolved = None

    _yfinance_search_cache[normalized_query] = (now, resolved)
    return resolved


def _strip_markdown_fences(text: str) -> str:
    cleaned = (text or "").strip()
    for start in ("```json", "```"):
        if cleaned.startswith(start):
            cleaned = cleaned[len(start) :].strip()
            break
    end_idx = cleaned.rfind("```")
    if end_idx >= 0:
        cleaned = cleaned[:end_idx].strip()
    return cleaned


def _parse_ai_resolution_payload(text: str) -> Optional[Dict[str, Any]]:
    """Parse the AI resolver JSON payload, tolerating fenced or slightly malformed output."""
    cleaned = _strip_markdown_fences(text)
    if not cleaned:
        return None

    candidates = [cleaned]
    json_start = cleaned.find("{")
    json_end = cleaned.rfind("}")
    if json_start >= 0 and json_end > json_start:
        candidates.append(cleaned[json_start : json_end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                from json_repair import repair_json

                data = repair_json(candidate, return_objects=True)
            except Exception:
                continue
        if isinstance(data, dict):
            return data

    return None


def _normalize_ai_confidence(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return 0.0
        mapping = {"high": 0.9, "medium": 0.65, "low": 0.35}
        if text in mapping:
            return mapping[text]
        try:
            return max(0.0, min(1.0, float(text)))
        except ValueError:
            return 0.0
    return 0.0


def _build_ai_resolution_messages(raw_input: str) -> List[Dict[str, str]]:
    system_prompt = (
        "You resolve user-entered stock or company names into canonical public-market identifiers. "
        "Return strict JSON only with keys: canonical_name, ticker, confidence, notes. "
        "Rules: "
        "1) If the input likely refers to a listed company, provide the best ticker/code. "
        "2) If unsure, ambiguous, private, or not publicly traded, return ticker=null and low confidence. "
        "3) Prefer the most common live market ticker such as AAPL, TSLA, KLAR, BRK.B, 600519, 00700. "
        "4) Do not include markdown or extra commentary."
    )
    user_prompt = json.dumps({"input": raw_input}, ensure_ascii=False)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _normalize_model_for_direct_call(model: str) -> str:
    text = (model or "").strip()
    if not text:
        return ""
    if "/" not in text:
        return f"openai/{text}"
    return text


def _first_configured_key(*env_names: str) -> str:
    for env_name in env_names:
        raw = (os.getenv(env_name) or "").strip()
        if not raw:
            continue
        for part in raw.split(","):
            key = part.strip()
            if key:
                return key
    return ""


def _build_direct_ai_call_kwargs(messages: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """Best-effort direct LiteLLM call when the runtime Config is not initialized yet."""
    try:
        from src.config import setup_env

        setup_env()
    except Exception:
        pass

    model = _normalize_model_for_direct_call(
        os.getenv("AI_NAME_RESOLVER_MODEL")
        or os.getenv("LITELLM_MODEL")
        or os.getenv("AGENT_LITELLM_MODEL")
        or ""
    )
    if not model:
        if _first_configured_key("DEEPSEEK_API_KEYS", "DEEPSEEK_API_KEY"):
            model = "deepseek/deepseek-chat"
        elif _first_configured_key("GEMINI_API_KEYS", "GEMINI_API_KEY"):
            model = "gemini/gemini-2.0-flash"
        elif _first_configured_key("ANTHROPIC_API_KEYS", "ANTHROPIC_API_KEY"):
            model = "anthropic/claude-3-5-sonnet-20241022"
        elif _first_configured_key("OPENAI_API_KEYS", "OPENAI_API_KEY", "AIHUBMIX_KEY"):
            model = _normalize_model_for_direct_call(os.getenv("OPENAI_MODEL") or "gpt-4o-mini")

    if not model:
        return None

    provider = model.split("/", 1)[0] if "/" in model else "openai"
    api_key = ""
    call_kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 160,
        "timeout": 15,
    }

    if provider in {"openai"}:
        api_key = _first_configured_key("OPENAI_API_KEYS", "OPENAI_API_KEY", "AIHUBMIX_KEY")
        api_base = (os.getenv("OPENAI_BASE_URL") or "").strip()
        if not api_base and (os.getenv("AIHUBMIX_KEY") or "").strip():
            api_base = "https://aihubmix.com/v1"
        if api_base:
            call_kwargs["api_base"] = api_base
            if "aihubmix.com" in api_base:
                call_kwargs["extra_headers"] = {"APP-Code": "GPIJ3886"}
    elif provider == "deepseek":
        api_key = _first_configured_key("DEEPSEEK_API_KEYS", "DEEPSEEK_API_KEY")
    elif provider in {"gemini", "vertex_ai"}:
        api_key = _first_configured_key("GEMINI_API_KEYS", "GEMINI_API_KEY")
    elif provider == "anthropic":
        api_key = _first_configured_key("ANTHROPIC_API_KEYS", "ANTHROPIC_API_KEY")

    if not api_key:
        return None

    call_kwargs["api_key"] = api_key
    return call_kwargs


def _call_ai_name_resolver(raw_input: str) -> Optional[Dict[str, Any]]:
    """Ask the configured LLM for a canonical stock hint, returning parsed JSON."""
    messages = _build_ai_resolution_messages(raw_input)

    try:
        from src.config import Config

        cfg = getattr(Config, "_instance", None)
    except Exception:
        cfg = None

    if cfg is not None:
        try:
            from src.agent.llm_adapter import LLMToolAdapter

            adapter = LLMToolAdapter(config=cfg)
            if adapter.is_available:
                response = adapter.call_text(messages, temperature=0, max_tokens=160, timeout=15)
                payload = _parse_ai_resolution_payload(response.content or "")
                if payload:
                    return payload
        except Exception as exc:
            logger.warning("[NameResolver] AI adapter fallback failed for %s: %s", raw_input, exc)

    try:
        import litellm

        call_kwargs = _build_direct_ai_call_kwargs(messages)
        if not call_kwargs:
            return None
        response = litellm.completion(**call_kwargs)
        content = ""
        if response and getattr(response, "choices", None):
            content = str(response.choices[0].message.content or "").strip()
        return _parse_ai_resolution_payload(content)
    except Exception as exc:
        logger.warning("[NameResolver] Direct AI fallback failed for %s: %s", raw_input, exc)
        return None


def _resolve_name_to_code_without_ai(name: str) -> Optional[str]:
    """Deterministic resolver path without any LLM fallback."""
    if not name or not isinstance(name, str):
        return None

    raw_input = name.strip()
    if not raw_input:
        return None

    if _is_code_like(raw_input):
        return _normalize_code(raw_input)

    local_reverse = _build_reverse_map_no_duplicates(STOCK_NAME_MAP)
    alias_reverse, normalized_alias_reverse = _build_alias_reverse_maps(
        STOCK_NAME_MAP,
        STOCK_NAME_ALIASES,
    )

    if raw_input in alias_reverse:
        return alias_reverse[raw_input]

    normalized_input = _normalize_lookup_name(raw_input)
    if normalized_input and normalized_input in normalized_alias_reverse:
        return normalized_alias_reverse[normalized_input]

    try:
        from pypinyin import lazy_pinyin

        input_pinyin = "".join(lazy_pinyin(raw_input)).lower()
        for local_name, code in local_reverse.items():
            local_pinyin = "".join(lazy_pinyin(local_name)).lower()
            if input_pinyin == local_pinyin:
                return code
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("[NameResolver] Pinyin match failed: %s", exc)

    akshare_map = _get_akshare_name_to_code()
    if akshare_map and raw_input in akshare_map:
        logger.debug("[NameResolver] Exact AkShare match: %s -> %s", raw_input, akshare_map[raw_input])
        return akshare_map[raw_input]

    foreign_code = _search_foreign_name_to_code(raw_input)
    if foreign_code:
        return foreign_code

    all_name_to_code = dict(alias_reverse)
    if akshare_map:
        all_name_to_code.update(akshare_map)

    if len(raw_input) > 2:
        names = list(all_name_to_code.keys())
        matches = difflib.get_close_matches(raw_input, names, n=1, cutoff=0.8)
        if matches:
            logger.debug("[NameResolver] Fuzzy match: input=%s matched=%s", raw_input, matches[0])
            return all_name_to_code[matches[0]]

        if normalized_input and len(normalized_input) > 2:
            normalized_names = list(normalized_alias_reverse.keys())
            normalized_matches = difflib.get_close_matches(
                normalized_input,
                normalized_names,
                n=1,
                cutoff=0.86,
            )
            if normalized_matches:
                return normalized_alias_reverse[normalized_matches[0]]

        typo_matches = difflib.get_close_matches(raw_input, names, n=1, cutoff=0.7)
        if typo_matches and _is_single_char_typo(raw_input, typo_matches[0]):
            logger.debug(
                "[NameResolver] Single-char typo fallback: input=%s matched=%s",
                raw_input,
                typo_matches[0],
            )
            return all_name_to_code[typo_matches[0]]

    logger.debug("[NameResolver] Resolve failed: %s", raw_input)
    return None


def _resolve_name_to_code_with_ai(name: str) -> Optional[str]:
    """LLM-assisted last-mile resolver for still-unresolved names."""
    raw_input = (name or "").strip()
    if not raw_input:
        return None
    if _is_local_ambiguous_input(raw_input):
        logger.info("[NameResolver] Skip AI fallback for ambiguous local input: %s", raw_input)
        return None

    normalized_query = _normalize_lookup_name(raw_input)
    if not normalized_query:
        return None

    now = time.time()
    cached = _ai_resolution_cache.get(normalized_query)
    if cached is not None and (now - cached[0]) < _AI_RESOLUTION_CACHE_TTL:
        return cached[1]

    payload = _call_ai_name_resolver(raw_input)
    if not payload:
        _ai_resolution_cache[normalized_query] = (now, None)
        return None

    confidence = _normalize_ai_confidence(payload.get("confidence"))
    candidate_values: List[str] = []
    for field in ("canonical_name", "company_name", "name"):
        value = payload.get(field)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned and cleaned not in candidate_values:
                candidate_values.append(cleaned)

    resolved: Optional[str] = None
    for candidate in candidate_values:
        resolved = _resolve_name_to_code_without_ai(candidate)
        if resolved:
            break

    if not resolved:
        raw_ticker = payload.get("ticker") or payload.get("code") or payload.get("symbol")
        if isinstance(raw_ticker, str):
            normalized_ticker = _normalize_code(raw_ticker)
            if normalized_ticker and confidence >= _AI_DIRECT_CODE_MIN_CONFIDENCE:
                resolved = normalized_ticker

    if resolved:
        logger.info(
            "[NameResolver] AI fallback resolved %s -> %s (confidence=%.2f)",
            raw_input,
            resolved,
            confidence,
        )
    else:
        logger.info(
            "[NameResolver] AI fallback could not confidently resolve %s (confidence=%.2f)",
            raw_input,
            confidence,
        )

    _ai_resolution_cache[normalized_query] = (now, resolved)
    return resolved


def resolve_name_to_code(name: str) -> Optional[str]:
    """
    Resolve a stock/company input to a canonical code.

    Strategy (in order):
    1. If input already looks like a code, normalize and return it.
    2. Local stock map / alias exact match.
    3. Normalized alias match (case-insensitive, suffix-insensitive).
    4. Pinyin exact match against local Chinese names.
    5. AkShare exact fallback for A-shares.
    6. Yahoo Finance search fallback for foreign company names.
    7. Conservative fuzzy matching.
    8. AI-assisted fallback for still-unresolved names.
    """
    resolved = _resolve_name_to_code_without_ai(name)
    if resolved:
        return resolved
    return _resolve_name_to_code_with_ai(name)


def resolve_stock_input(value: str) -> Optional[str]:
    """Semantic alias for resolving a user-supplied stock input."""
    return resolve_name_to_code(value)


def resolve_stock_inputs(values: Iterable[str]) -> Tuple[List[str], List[str]]:
    """
    Resolve a list of user inputs into canonical stock codes.

    Returns:
        (resolved_codes, unresolved_inputs)
    """
    resolved_codes: List[str] = []
    unresolved_inputs: List[str] = []

    for value in values:
        raw = (value or "").strip()
        if not raw:
            continue
        resolved = resolve_stock_input(raw)
        if resolved:
            resolved_codes.append(resolved)
        else:
            unresolved_inputs.append(raw)

    return resolved_codes, unresolved_inputs
