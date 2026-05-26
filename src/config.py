DEFAULT_ASSUMPTIONS = {
    # Sale timeline
    "target_sale_year": 2029,
    "analysis_year": 2026,

    # Market appreciation — conservative standard assumption
    "annual_appreciation": 0.03,

    # Cost model
    "construction_cost_per_sqft": 800,
    "additional_lot_cost_rate": 0.045,     # 4.5 % of buying price

    # New-build premium over comps — conservative; comps are mostly resales
    "new_construction_premium": 0.12,

    # View premiums — kept small because comp selection already matches on view type
    # (25 pts in comp scorer). These are residual adjustments only.
    "ocean_view_premium": 0.15,
    "canyon_view_premium": 0.10,
    "mountain_view_premium": 0.07,
    "city_view_premium": 0.05,
    "partial_view_premium": 0.05,

    # Privacy premiums — not in comp scorer so more defensible as adjustments
    "very_high_privacy_premium": 0.10,
    "high_privacy_premium": 0.06,
    "medium_privacy_premium": 0.04,

    # Usability premiums (topography) — buildability is genuinely comp-independent
    "flat_usability_premium": 0.05,
    "gentle_usability_premium": 0.02,

    # Slope / complexity discounts
    "moderate_slope_discount": 0.05,
    "steep_slope_discount": 0.12,

    # Comp price threshold (flexible — $3M default, $5M strict luxury)
    "min_comp_sale_price": 3_000_000,

    # Neighborhood premiums — tight spread because comp selection already weights
    # neighborhood heavily (30 pts). These are residual prestige adjustments only.
    "riviera_premium":          0.13,
    "castellammare_premium":    0.12,
    "highlands_premium":        0.11,
    "upper_palisades_premium":  0.10,
    "village_premium":          0.10,
    "marquez_knolls_premium":   0.08,
    "default_neighborhood_premium": 0.05,
}

SCORING_WEIGHTS = {
    "view_score": 0.25,
    "privacy_score": 0.20,
    "usability_score": 0.15,
    "location_prestige": 0.15,
    "price_attractiveness": 0.10,
    "buildability_score": 0.10,
    "comp_confidence_score": 0.05,
}
