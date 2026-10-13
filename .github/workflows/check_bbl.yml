name: Patikrinti BBL

on:
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Įdiegti priklausomybes
        run: |
          sudo apt-get update
          sudo apt-get install -y tesseract-ocr poppler-utils
          pip install requests

      - name: Patikra
        run: python check_bbl.py
