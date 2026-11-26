"""Model SKU parser, model ID normalizer, and billing export SQL clause helpers."""

from __future__ import annotations

import re

from ..schema import expr

QUALIFIER_RE = re.compile(
    r"(?i)(?:<=|>=|=<|=>|<|>|\u2264|\u2265|up to|over|above|below)\s*\d+(?:\.\d+)?\s*[km]\b"
    r"(?:\s*(?:input\s+)?tokens?)?"
)
FOOTNOTE_RE = re.compile(
    r"(?i)(?:\*+.*|[\u00b9\u00b2\u00b3\u2070-\u2079]+.*"
    r"|(?:\b|(?<=[a-z0-9]))(?:through|starting|effective)\s+"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b.*"
    r"|\bwhen\s+.*)"
)
WORD_SPLIT_RE = re.compile(r"[\s_\-\u2013\u2014,:;/|\[\]{}]+")
PAREN_RE = re.compile(r"\(([^)]*)\)")
DIGIT_RE = re.compile(r"\d")

FAMILIES = {
    "gemini", "gemma", "medgemma", "claude", "llama", "mistral", "mixtral", "codestral",
    "ministral", "pixtral", "imagen", "veo", "chirp", "lyria", "jamba", "deepseek", "qwen", "gpt", "embedding",
}
NON_TOKEN_FAMILIES = frozenset({"imagen", "veo", "chirp", "lyria"})
TIERS = {
    "pro", "flash", "lite", "ultra", "nano", "omni", "sonnet", "opus", "haiku", "instant",
    "large", "medium", "small", "fast", "oss", "live", "embedding", "native", "image", "tts",
    "hd", "clip", "coder", "maverick", "scout",
}
STOP_WORDS = {
    "text", "input", "output", "caching", "cached", "cache", "token", "tokens", "count",
    "predictions", "prediction", "generation", "generations", "characters", "character",
    "seconds", "second", "requests", "request", "read", "write", "ttl", "context", "window",
    "size", "from", "to", "video", "audio", "music", "search", "query", "queries", "grounding",
    "with", "for", "and", "per", "in", "on", "of", "by", "images", "storage", "training",
    "tuning", "endpoint", "hour", "hours", "node", "usage", "calls", "call",
}
SKIP_WORDS = {
    "global", "regional", "ga", "preview", "short", "long", "thinking", "priority",
    "flex", "standard", "batch", "online", "free", "latest", "exp", "experimental",
}
MODALITIES = {"text", "image", "audio", "video"}
GENERIC_WORDS = {
    "generate", "content", "generatecontent", "bidi", "batch", "online", "predict", "prediction",
    "predictions", "token", "tokens", "count", "input", "output", "cached", "cache", "caching",
    "text", "image", "audio", "video", "for", "the", "search", "query", "grounding", "request",
    "requests", "live", "streaming", "stream", "api", "model", "models", "vertex", "ai", "garden",
    "a", "an", "cloud", "tts", "speech", "anthropic", "google", "meta", "openai", "ai21", "labs",
    "nvidia", "partner", "maas",
}
ACRONYMS = {"tts": "TTS", "hd": "HD", "gpt": "GPT", "oss": "OSS", "ai": "AI", "api": "API"}

AI_SERVICE_RE = (
    r"(?i)(vertex ai|agent platform|gemini|generative|text-to-speech|speech-to-text|"
    r"cloud speech|anthropic|claude|model garden|llama|mistral|ai platform)"
)
AI_SKU_RE = r"(?i)(gemini|claude|llama|mistral|gemma|imagen|veo|chirp|lyria|token)"
INFRA_SKU_RE = (
    r"(?i)(data index|reasoningengine|reasoning engine|agent engine|vertex ai search|vcpu|memory|"
    r"storage|training|endpoint|node hour)"
)


def _base_id(model_id: str) -> str:
    mid = (model_id or "").strip().lower()
    if "/" in mid:
        mid = mid.rsplit("/", 1)[-1]
    mid = FOOTNOTE_RE.sub("", mid).strip()
    mid = mid.split("@", 1)[0]
    mid = re.sub(r"[\s_]+", "-", mid)
    mid = re.sub(r"-\d{8}$", "", mid)
    mid = re.sub(r"-\d{2}-\d{4}$", "", mid)
    mid = re.sub(r"-(?:preview|experimental|exp(?:-\d+)?|latest)(?=-|$)", "", mid)
    mid = re.sub(r"-\d{2}-\d{4}$", "", mid)
    mid = re.sub(r"-native-image(?=-|$)", "-image", mid)
    if "embedding" not in mid:
        mid = re.sub(r"-\d{3}$", "", mid)
    if mid.startswith("claude-") and "." not in mid:
        mid = re.sub(r"(?<=-)(\d+)-(\d)(?=-|$)", r"\1.\2", mid)
    return re.sub(r"-{2,}", "-", mid).strip("-")


def normalize_model_id(model_id: str) -> str:
    return re.sub(r"(?<=-)(\d+)\.0(?=-|$)", r"\1", _base_id(model_id))


def is_non_token_model(model_or_key: str) -> bool:
    key = normalize_model_id(str(model_or_key or ""))
    return bool(key) and key.split("-", 1)[0] in NON_TOKEN_FAMILIES


def display_from_key(key: str) -> str:
    words = []
    for part in (key or "").split("-"):
        if not part:
            continue
        if part in ACRONYMS:
            words.append(ACRONYMS[part])
        elif re.match(r"^\d", part):
            words.append(part)
        else:
            words.append(part[:1].upper() + part[1:])
    return " ".join(words)


def _model_words(low: list[str], i: int, for_form: bool):
    j = i + 1
    if j >= len(low) or not (DIGIT_RE.search(low[j]) or low[j] in TIERS):
        return None
    out = [low[i]]
    if low[i] == "embedding" and i > 0 and low[i - 1] in ("text", "multimodal"):
        out.insert(0, low[i - 1])
    while j < len(low):
        word = low[j]
        nxt = j + 1
        while nxt < len(low) and low[nxt] in SKIP_WORDS:
            nxt += 1
        following = low[nxt] if nxt < len(low) else None
        if word in SKIP_WORDS:
            j += 1
            continue
        if word == "native" and following in ("image", "audio"):
            out.extend([word, following])
            j = nxt + 1
            continue
        if word == "image":
            if "image" not in out and (
                following in MODALITIES
                or (for_form and (following is None or following in STOP_WORDS))
                or (low[i] == "gemini" and (following in ("output", "generation", "generations")
                                            or (i == 0 and following is None)))
            ):
                out.append(word)
                j += 1
                continue
            break
        if word in STOP_WORDS:
            break
        out.append(word)
        j += 1
    return out


def is_model_service(service: str) -> bool:
    words = [w for w in WORD_SPLIT_RE.split(service or "") if w]
    return bool(words) and any(DIGIT_RE.search(w) for w in words) and any(w.isalpha() for w in words)


def parse_sku(description: str, service: str | None = None) -> dict | None:
    """Identifies the AI model behind a Cloud Billing SKU description."""
    if not description or not isinstance(description, str):
        return None
    text = FOOTNOTE_RE.sub(" ", QUALIFIER_RE.sub(" ", description))
    parens = PAREN_RE.findall(text)
    original = [w.strip(".") for w in WORD_SPLIT_RE.split(PAREN_RE.sub(" ", text)) if w.strip(".")]
    low = [w.lower() for w in original]
    for_form = False
    product, brand_family, model, model_idx = [], None, None, len(low)
    for i, word in enumerate(low):
        if word in FAMILIES:
            model = _model_words(low, i, for_form)
            if model:
                model_idx = i
                break
            brand_family = brand_family or word
            product.append(original[i])
            continue
        if word == "for":
            for_form = True
            continue
        if word in STOP_WORDS:
            if product and brand_family:
                break
            continue
        if word not in GENERIC_WORDS and word not in SKIP_WORDS and not DIGIT_RE.search(word):
            product.append(original[i])
    if model is None:
        for group in parens:
            gl = [w.lower().strip(".") for w in WORD_SPLIT_RE.split(group or "") if w.strip(".")]
            for idx, w in enumerate(gl):
                if w in FAMILIES:
                    model = _model_words(gl, idx, False)
                    if model:
                        break
            if model:
                break
    if model is None:
        if not (service and is_model_service(service)):
            return None
        model = [w.lower() for w in WORD_SPLIT_RE.split(service) if w and w.lower() not in SKIP_WORDS]
        product = []
    if (model and model[0] == "gemini" and "tts" not in model
            and ("tts" in low[:model_idx] or "text-to-speech" in (service or "").lower())):
        model = list(model) + ["tts"]
    display_key = _base_id("-".join(model))
    if not display_key:
        return None
    return {
        "key": normalize_model_id(display_key),
        "name": display_from_key(display_key),
        "product": " ".join(product) if product else None,
    }


def classify_measure(description: str, unit: str | None = None, pricing_unit: str | None = None) -> str:
    """Classifies a billing SKU into input_tokens, output_tokens, cached_tokens, or other."""
    low = (description or "").lower()
    u = (unit or "").lower()
    pu = (pricing_unit or "").lower()
    is_token = "token" in low or "token" in pu or (u == "count" and ("input" in low or "output" in low or "cach" in low))
    if not is_token:
        return "other"
    if "cach" in low and "storage" not in low and "write" not in low:
        return "cached_tokens"
    if "output" in low or "candidate" in low or "thinking" in low:
        return "output_tokens"
    if "input" in low or "prompt" in low:
        return "input_tokens"
    return "other"


def billing_fields(src, alias: str = "b") -> dict:
    s = src.schema
    return {
        "sku": expr(alias, s, "sku.description", cast="STRING"),
        "service": expr(alias, s, "service.description", cast="STRING"),
        "amount": expr(alias, s, "usage.amount", cast="FLOAT64"),
        "unit": expr(alias, s, "usage.unit", cast="STRING"),
        "pricing_unit": expr(alias, s, "usage.pricing_unit", cast="STRING"),
        "cost": expr(alias, s, "cost", cast="FLOAT64"),
        "currency": expr(alias, s, "currency", cast="STRING"),
        "project": expr(alias, s, "project.id", cast="STRING"),
        "ts": "%s.`usage_start_time`" % alias,
    }


def excluded_clause(f: dict) -> str:
    sku = "LOWER(IFNULL(%s, ''))" % f["sku"]
    svc = "LOWER(IFNULL(%s, ''))" % (f["service"] or "''")
    return ("NOT EXISTS (SELECT 1 FROM UNNEST(@excluded) AS p WHERE STRPOS(%s, p) > 0 OR STRPOS(%s, p) > 0)"
            % (sku, svc))


def ai_clause(f: dict) -> str:
    svc = "IFNULL(%s, '')" % (f["service"] or "''")
    sku = "IFNULL(%s, '')" % f["sku"]
    return ("(REGEXP_CONTAINS(%s, r'%s') OR REGEXP_CONTAINS(%s, r'%s')) AND NOT REGEXP_CONTAINS(%s, r'%s')"
            % (svc, AI_SERVICE_RE, sku, AI_SKU_RE, sku, INFRA_SKU_RE))
