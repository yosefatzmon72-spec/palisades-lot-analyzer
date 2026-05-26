from pathlib import Path

lines = [
    'address,asking_price,lot_size_sqft,prev_home_sqft,prev_beds,prev_baths,prev_sale_price,assessed_value,fire_status,listing_description,zillow_url,redfin_url,view_quality,privacy,topography,street_quality,neighborhood,distance_to_ocean_miles,usable_lot_area_sqft,zoning,buildable_sqft,listing_source',
    '"123 Ocean Vista Dr, Pacific Palisades, CA",5500000,15000,3800,4,5,8000000,7200000,fire damaged,"Fire damaged hillside lot with canyon and ocean glimpses. Private gated entry. Potential for 8,500 sqft estate.",https://zillow.example.com/123ocean,https://redfin.example.com/123ocean,ocean,high,steep,excellent,Upper Palisades,0.9,9000,R1,8500,Zillow',
    '"45 Canyon Ridge Ln, Pacific Palisades, CA",4200000,12500,3200,3,4,6000000,5000000,burned vacant,"Burned vacant parcel with strong canyon exposure and quiet street. Near village and private access.",https://zillow.example.com/45canyon,https://redfin.example.com/45canyon,canyon,medium,moderate,good,Lower Palisades,2.5,7000,R1,7600,Redfin',
    '"89 Palisades Dr, Pacific Palisades, CA",6300000,18000,4000,5,5,7200000,7500000,vacant rebuild,"Vacant rebuild lot with ocean view, private drive, and flat build pad. Recent demolition completed.",https://zillow.example.com/89pali,https://redfin.example.com/89pali,ocean,very_high,flat,excellent,Upper Palisades,1.2,12000,R1,9500,MLS',
]
Path('data/lots.csv').write_text('\n'.join(lines) + '\n', encoding='utf-8')
