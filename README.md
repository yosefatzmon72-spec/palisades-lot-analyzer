# Palisades Lot Analyzer

A Streamlit-based MVP for analyzing burned, vacant, and rebuildable lots in Pacific Palisades.

## Features
- CSV import for lot listings and luxury comps
- Lot scoring by view, privacy, usability, location prestige, and price attractiveness
- Luxury comp engine using pre-2025 transactions
- Future resale valuation for 2028/2029
- Cost and profit model with editable assumptions
- Ranked top 15 lots by profit percentage
- Investment memo generation for each lot

## Project structure

```
palisades-lot-analyzer/
  app.py
  data/
    lots.csv
    comps.csv or comps.xlsx
  outputs/
    top_15_lots.csv
    investment_memos/
  src/
    config.py
    data_loader.py
    description_parser.py
    comp_engine.py
    scoring_model.py
    valuation_model.py
    report_generator.py
  README.md
  requirements.txt
```

## Setup

```bash
cd ~/palisades-lot-analyzer
python3 -m pip install -r requirements.txt
streamlit run app.py
```

## Usage

- Upload `lots.csv` and `comps.csv` or `comps.xlsx` in the app
- Adjust financial assumptions in the sidebar
- View ranked lots and download the top 15 CSV
- Generate investment memos for recommended deals
