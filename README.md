# Metro Atlanta Tax Sale Tracker

Live dashboard: **https://shamarrobinson.github.io/atlanta-tax-sale-dashboard/**

Delinquent property tax sales for Fulton County and every Georgia county that falls even partly inside a 52-mile circle around downtown Atlanta. That radius reaches Athens-Clarke, Hall, Bartow, Fayette, Paulding, Douglas and Newton, and takes in 40 counties in all.

## What it shows
- **Region overview:** a map of every county in the radius, upcoming sales, a county table and winning bids compared across counties.
- **A tab per county** (with its own link, e.g. `#gwinnett`):
  - Who runs the sale, the schedule, and links to the county's tax sale page, excess funds and parcel lookup.
  - An auction dropdown with every sale on file (upcoming lists, published results and excess funds lists).
  - Bid stats: average, median, lowest, highest and most frequent winning bid.
  - Cost tiers, auction history, bidding multiples and the most active buyers.
  - A map of geocoded parcels.
  - A parcel grid with sorting, column controls, pivots on any column and CSV download.
- **Search** across every county and field, e.g. `county:gwinnett won>100000`.

## Where the data comes from
Each county's Tax Commissioner or Sheriff website. Georgia counties publish very different things:
- **Full results with winning bids:** Gwinnett, Carroll and Fayette (Jackson only back to 2017).
- **Excess funds lists (parcels that sold above what was owed; most include the sale price):** Cobb, DeKalb, Clayton, Coweta, Walton, Forsyth, Polk, Meriwether, Pickens, Athens-Clarke, Troup, Lumpkin and Dawson.
- **Sale lists only:** Fulton (scanned, read with OCR), Henry, Douglas, Rockdale, Spalding, Floyd and Heard.
- **Nothing parcel-level online:** the remaining counties. Their tabs still show the schedule and official links.

`fetch_all.py` runs every Monday through GitHub Actions. It reads each county's files, merges them with what was saved before (so a list stays in the archive after the county removes it), geocodes street addresses with the U.S. Census geocoder, and commits `data/`.

## Files
- `registry.py`: every county, its links and which parser reads it.
- `counties_metro.py`, `counties_outer.py`: one parser per county.
- `ga_common.py`: download, PDF/OCR/spreadsheet helpers.
- `build_region.py`: builds the county list from Census boundaries (52-mile circle around downtown Atlanta).
- `fetch_all.py`: runs everything and writes `data/*.json`.
- `index.html`, `app.js`, `style.css`, `atl.css`: the dashboard (plain HTML/JS, Chart.js and Leaflet).

Not affiliated with any county. Lists change up to the day of the sale; always confirm with the county.
