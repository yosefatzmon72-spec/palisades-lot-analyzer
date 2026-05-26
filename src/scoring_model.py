from .config import SCORING_WEIGHTS


# ── Individual dimension scorers ─────────────────────────────────────────────

def map_view_quality(value: str) -> float:
    return {"ocean": 100, "canyon": 90, "city": 70, "mountain": 80,
            "partial": 55, "none": 30}.get(str(value).lower(), 40)


def map_privacy(value: str) -> float:
    return {"very_high": 100, "high": 85, "medium": 65,
            "low": 40}.get(str(value).lower(), 50)


def map_topography(value: str) -> float:
    return {"flat": 100, "gentle": 85, "moderate": 65,
            "steep": 35}.get(str(value).lower(), 50)


def map_street_quality(value: str) -> float:
    return {"excellent": 100, "good": 85, "fair": 60,
            "poor": 30}.get(str(value).lower(), 50)


def map_neighborhood(value: str) -> float:
    return {"upper palisades": 100, "village": 95,
            "lower palisades": 85, "custom": 70}.get(str(value).lower(), 65)


# ── Buildability score ───────────────────────────────────────────────────────

def score_buildability(lot_row, build_sqft_confidence: str = "low") -> float:
    """
    0–100.  Higher = easier and more certain to build.

    Factors: topography (40 %), street quality (25 %), lot size (25 %),
             fire/permit status bonus (10 %).
    """
    topo_score = map_topography(lot_row.get("topography", "moderate"))
    street_score = map_street_quality(lot_row.get("street_quality", "good"))

    lot_sqft = float(lot_row.get("lot_size_sqft") or 0)
    if lot_sqft >= 15_000:
        size_score = 100
    elif lot_sqft >= 10_000:
        size_score = 85
    elif lot_sqft >= 7_500:
        size_score = 70
    elif lot_sqft >= 5_000:
        size_score = 55
    else:
        size_score = 35 if lot_sqft > 0 else 20

    base = topo_score * 0.40 + street_score * 0.25 + size_score * 0.25

    # Bonus for cleared/permitted status
    fire = str(lot_row.get("fire_status", "")).lower()
    has_permit = float(lot_row.get("buildable_sqft") or 0) > 0
    if has_permit:
        bonus = 15
    elif any(k in fire for k in ("burn", "vacant", "clear", "demol")):
        bonus = 8
    else:
        bonus = 0

    # Confidence penalty for estimated sqft
    conf_penalty = {"high": 0, "medium": 3, "low": 8, "none": 15}.get(
        build_sqft_confidence, 5
    )

    return max(0.0, min(100.0, base + bonus - conf_penalty))


# ── Risk score ───────────────────────────────────────────────────────────────

def score_risk(lot_row, comp_info: dict | None = None) -> tuple:
    """
    Returns (risk_score: float 0–100, risk_factors: list[str]).

    Higher = riskier.  Factors: topography, privacy, view, comp support,
    acquisition cost.
    """
    risk = 0.0
    factors = []

    topo = str(lot_row.get("topography", "moderate")).lower()
    if topo == "steep":
        risk += 25
        factors.append("Steep topography (+25)")
    elif topo == "moderate":
        risk += 10
        factors.append("Moderate slope (+10)")

    if str(lot_row.get("privacy", "medium")).lower() == "low":
        risk += 12
        factors.append("Low privacy (+12)")

    if str(lot_row.get("view_quality", "none")).lower() == "none":
        risk += 10
        factors.append("No view (+10)")

    if comp_info:
        conf = comp_info.get("comp_confidence", 0.5)
        if conf < 0.3:
            risk += 20
            factors.append("Very weak comp support (+20)")
        elif conf < 0.6:
            risk += 10
            factors.append("Limited comp support (+10)")

    asking = float(lot_row.get("asking_price") or 0)
    if asking > 8_000_000:
        risk += 15
        factors.append("Very high acquisition cost (+15)")
    elif asking > 6_000_000:
        risk += 8
        factors.append("High acquisition cost (+8)")

    return min(100.0, risk), factors


# ── Master scorer ─────────────────────────────────────────────────────────────

def score_lot(lot_row, comp_ppsf, comp_info: dict | None = None,
              build_sqft_confidence: str = "low") -> dict:
    view_score = map_view_quality(lot_row.get("view_quality", "none"))
    privacy_score = map_privacy(lot_row.get("privacy", "low"))
    usability_score = (
        map_topography(lot_row.get("topography", "moderate"))
        + map_street_quality(lot_row.get("street_quality", "good"))
    ) / 2
    location_score = map_neighborhood(lot_row.get("neighborhood", "custom"))
    buildability_score = score_buildability(lot_row, build_sqft_confidence)
    risk_score, risk_factors = score_risk(lot_row, comp_info)

    # Price attractiveness vs implied comp value
    price_attractiveness = 50.0
    asking = lot_row.get("asking_price", 0)
    buildable = lot_row.get("buildable_sqft", 0)
    if asking and comp_ppsf and comp_ppsf > 0 and buildable > 0:
        implied_sale = comp_ppsf * buildable
        ratio = asking / implied_sale if implied_sale > 0 else 1.0
        price_attractiveness = max(0.0, min(100.0, 100 - (ratio - 0.6) * 120))

    comp_conf_score = (comp_info.get("comp_confidence", 0.5) if comp_info else 0.5) * 100

    overall = (
        view_score         * SCORING_WEIGHTS.get("view_score",          0.25)
        + privacy_score    * SCORING_WEIGHTS.get("privacy_score",        0.20)
        + usability_score  * SCORING_WEIGHTS.get("usability_score",      0.15)
        + location_score   * SCORING_WEIGHTS.get("location_prestige",    0.15)
        + price_attractiveness * SCORING_WEIGHTS.get("price_attractiveness", 0.10)
        + buildability_score * SCORING_WEIGHTS.get("buildability_score", 0.10)
        + comp_conf_score  * SCORING_WEIGHTS.get("comp_confidence_score", 0.05)
    )

    return {
        "view_score": view_score,
        "privacy_score": privacy_score,
        "usability_score": usability_score,
        "location_score": location_score,
        "buildability_score": buildability_score,
        "price_attractiveness": price_attractiveness,
        "comp_confidence_score": comp_conf_score,
        "risk_score": risk_score,
        "risk_factors": " | ".join(risk_factors) if risk_factors else "None identified",
        "overall_score": overall,
    }
