# De Soto Kansas City Code Scraper

Scrapes all pages from the [Code of the City of De Soto, Kansas](https://desotokansas.citycode.net/index.html#!codeOfTheCityOfDeSotoKansas) and consolidates them into a single PDF.

## How It Works

The citycode.net site is a JavaScript single-page application that uses hashbang (`#!`) routing. Each route loads a `.htm` content file via AJAX. This scraper:

1. **Discovers** all section links from the Table of Contents page
2. **Renders** each section in a headless Chromium browser (via Playwright)
3. **Generates** a per-section PDF from the rendered page
4. **Merges** all section PDFs into one consolidated document

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
# Basic usage — outputs desoto_city_code.pdf
python3 scraper.py

# Custom output path
python3 scraper.py -o my_output.pdf

# Keep temporary per-section PDFs for inspection
python3 scraper.py --keep-temp

# Lower concurrency if the site rate-limits
python3 scraper.py --concurrency 1

# Verbose debug logging
python3 scraper.py -v
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `-o`, `--output` | `desoto_city_code.pdf` | Output PDF file path |
| `--temp-dir` | `temp_pdfs` | Directory for temporary per-section PDFs |
| `--keep-temp` | off | Keep temporary PDFs after merging |
| `--concurrency` | 3 | Max concurrent page renders |
| `-v`, `--verbose` | off | Enable debug logging |
