"""
Parse free-text listing descriptions for structured lot signals.

Extracts numeric values (price, sqft, etc.) and categorical flags
(view type, privacy, slope, permit status, fire status).
All extracted values are tagged with confidence so callers can decide
whether to trust the result or label it as an assumption.
"""
import re


# ── Numeric extraction helpers ──────────────────────────────────────────────

def _first_number(text: str, patterns: list) -> float | None:
    """Return first positive float matched by any of the regex patterns."""
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(",", "")
            try:
                return float(raw)
            except ValueError:
                continue
    return None


def _extract_price(text: str) -> float | None:
    """Extract dollar amounts that look like asking prices (≥ $500 K)."""
    patterns = [
        r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:million|m\b)",   # $5 million
        r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:k\b)",            # $5,500K
        r"asking\s+\$?\s*([\d,]+)",
        r"price[d]?\s+at\s+\$?\s*([\d,]+)",
        r"listed\s+(?:at|for)\s+\$?\s*([\d,]+)",
        r"\$\s*([\d,]{6,})",                             # bare $5,500,000
    ]
    val = _first_number(text, patterns)
    if val is None:
        return None
    # Normalize: "5 million" → 5_000_000
    if re.search(r"million|m\b", text, re.IGNORECASE) and val < 100:
        val *= 1_000_000
    elif re.search(r"\bk\b", text, re.IGNORECASE) and val < 10_000:
        val *= 1_000
    return val if val >= 500_000 else None


def _extract_sqft(text: str, keywords: list) -> float | None:
    """Extract square footage near one of the given keywords."""
    kw_pat = "|".join(re.escape(k) for k in keywords)
    patterns = [
        rf"([\d,]+)\s*(?:sq\.?\s*ft|sqft|sf)\s+(?:{kw_pat})",
        rf"(?:{kw_pat})[^.]*?([\d,]+)\s*(?:sq\.?\s*ft|sqft|sf)",
        rf"(?:{kw_pat})[:\s]+approximately\s+([\d,]+)",
    ]
    return _first_number(text, patterns)


# ── Main parser ─────────────────────────────────────────────────────────────

def parse_description_signals(text: str) -> dict:
    """
    Parse a listing description string and return a dict of extracted signals.

    Confidence levels:
      "explicit"   — number or keyword found verbatim in text
      "inferred"   — derived from contextual clues
      "assumed"    — default used because no signal found
    """
    t = text.lower() if text else ""

    signals = {}

    # ── View type ────────────────────────────────────────────────────────────
    if re.search(r"ocean view|pacific view|ocean glimps|water view", t):
        signals["view_quality"] = ("ocean", "explicit")
    elif re.search(r"canyon view|canyon exposure|canyon facing", t):
        signals["view_quality"] = ("canyon", "explicit")
    elif re.search(r"city view|cityscape|city lights", t):
        signals["view_quality"] = ("city", "explicit")
    elif re.search(r"mountain view|mountain exposure|mountain facing", t):
        signals["view_quality"] = ("mountain", "explicit")
    elif re.search(r"partial view|peek|peekaboo", t):
        signals["view_quality"] = ("partial", "inferred")

    # ── Privacy ──────────────────────────────────────────────────────────────
    if re.search(r"gated estate|private gate|estate entry|fully private|secluded", t):
        signals["privacy"] = ("very_high", "explicit")
    elif re.search(r"private|gated|cul.de.sac|end of road|cul de sac", t):
        signals["privacy"] = ("high", "explicit")
    elif re.search(r"semi.private|quiet street|low traffic", t):
        signals["privacy"] = ("medium", "inferred")

    # ── Topography / slope ───────────────────────────────────────────────────
    if re.search(r"flat\s+(?:lot|pad|build|grade|terrain)|level\s+(?:lot|pad)|flat build", t):
        signals["topography"] = ("flat", "explicit")
    elif re.search(r"gentle\s+slope|gradual\s+slope|mild\s+slope", t):
        signals["topography"] = ("gentle", "explicit")
    elif re.search(r"steep\s+(?:slope|lot|hill|terrain|access)|hillside lot|challenging\s+grade|vertical\s+lot", t):
        signals["topography"] = ("steep", "explicit")
    elif re.search(r"slope|sloped|hillside|terraced|graded", t):
        signals["topography"] = ("moderate", "inferred")

    # ── Fire / rebuild status ────────────────────────────────────────────────
    if re.search(r"burned|fire damaged|fire loss|palisades fire|eaton fire", t):
        signals["fire_status"] = ("fire_damaged", "explicit")
    elif re.search(r"vacant lot|bare land|cleared|demolished|demolition complete", t):
        signals["fire_status"] = ("vacant", "explicit")
    elif re.search(r"rebuild|reconstruction|new construction|scrape", t):
        signals["fire_status"] = ("rebuild_opportunity", "inferred")

    # ── Buildability signals ─────────────────────────────────────────────────
    if re.search(r"\brti\b|ready to issue|ready-to-issue", t):
        signals["has_rti"] = (True, "explicit")
    if re.search(r"plans approved|approved plans|city approved|permit approved", t):
        signals["has_approved_plans"] = (True, "explicit")
    if re.search(r"build pad|building pad|graded pad|pad ready|flat build pad", t):
        signals["has_build_pad"] = (True, "explicit")
    if re.search(r"development opportunity|shovel.ready|ready to build", t):
        signals["is_development_ready"] = (True, "inferred")

    # ── Square footage signals ───────────────────────────────────────────────
    permitted_sqft = _extract_sqft(t, ["permitted", "permit", "rti", "approved"])
    if permitted_sqft:
        signals["buildable_sqft"] = (permitted_sqft, "explicit")

    pre_fire_sqft = _extract_sqft(t, ["previous", "prior", "pre-fire", "former", "original"])
    if pre_fire_sqft:
        signals["prev_home_sqft"] = (pre_fire_sqft, "explicit")

    # Generic sqft mention as fallback
    generic_sqft = _first_number(t, [r"([\d,]+)\s*(?:sq\.?\s*ft|sqft|sf)\b"])
    if generic_sqft and generic_sqft >= 1000:
        if "buildable_sqft" not in signals and "prev_home_sqft" not in signals:
            signals["est_home_sqft"] = (generic_sqft, "inferred")

    # ── Price hint ───────────────────────────────────────────────────────────
    price_hint = _extract_price(text)
    if price_hint:
        signals["price_hint"] = (price_hint, "explicit")

    # ── Usability signals ────────────────────────────────────────────────────
    signals["has_ocean_view"] = re.search(r"ocean view|pacific view|ocean glimps", t) is not None
    signals["has_canyon_view"] = re.search(r"canyon view|canyon exposure", t) is not None
    signals["has_city_view"] = re.search(r"city view|cityscape", t) is not None
    signals["has_mountain_view"] = re.search(r"mountain view", t) is not None
    signals["is_private"] = re.search(r"private|gated|secluded|cul.de.sac|estate entry", t) is not None
    signals["is_buildable"] = re.search(
        r"build pad|buildable|rebuild|development opportunity|rti|plans approved|shovel.ready", t
    ) is not None
    signals["is_burned"] = re.search(
        r"burned|fire damaged|vacant|demolition|burned structure|palisades fire", t
    ) is not None

    return signals


def apply_description_signals_to_lot(lot_row: dict, signals: dict) -> dict:
    """
    Fill missing lot fields from parsed description signals.
    Only overwrites if the field is zero / empty — never overwrites real data.
    Returns an updated copy of lot_row with an `assumptions` list attached.
    """
    row = dict(lot_row)
    assumptions = list(row.get("assumptions", []))

    for field, (value, confidence) in [
        (k, v) for k, v in signals.items()
        if isinstance(v, tuple) and len(v) == 2
    ]:
        if field in ("has_ocean_view", "has_canyon_view", "has_city_view",
                     "has_mountain_view", "is_private", "is_buildable", "is_burned",
                     "price_hint", "est_home_sqft"):
            continue  # these are informational, not direct field overrides

        current = row.get(field)
        is_empty = (
            current is None
            or (isinstance(current, float) and current != current)  # NaN
            or (isinstance(current, (int, float)) and current == 0)
            or (isinstance(current, str) and current.strip() == "")
        )
        if is_empty:
            row[field] = value
            assumptions.append(f"{field}={value} ({confidence}, from description)")

    row["assumptions"] = assumptions
    return row
