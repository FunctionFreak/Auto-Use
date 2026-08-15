
# Goal
Convert "/Users/ashishyadav/Downloads/Sales and Return Register Report (22).xlsx" into a Tally-importable XML file, saved in Downloads.

# Findings (from minion)
- Sheets: Summary (skip), Sales Register (header row2, data row3-1149), Sales Return (header row2, data row3-411).
- Sales Register cols: A Invoice NO, D Invoice Date(serial), F Salesperson, G Customer, H GST NO, I Untaxed Amt, J Tax Amount, K Total Amt, L Local Sales, plus per-rate GST cols (M-AF) e.g. SGST/CGST/IGST Output x% DL/MH.
- Sales Return cols: A Invoice NO(prefix R), B Invoice Date(serial), D Salesperson, E Customer, F GST NO, G Untaxed Amt, H Tax Amount, I Total Amt, J Local Sales, plus per-rate GST cols.
- No line items — invoice-level only. Map to ledger-level Tally voucher entries.
- Dates are Excel serial ints -> convert with epoch 1899-12-30.

# Mapping design
- Sales Register row -> Tally "Sales" voucher:
  - Party ledger (Customer) DEBIT Total Amt
  - "Sales Local" ledger CREDIT Untaxed/Local Sales Amt (use Local Sales col if present else Untaxed Amt)
  - For each nonzero GST column (SGST/CGST/IGST Output x%) -> CREDIT that GST ledger name with its amount
- Sales Return row -> Tally "Credit Note" voucher (reverse):
  - Party ledger CREDIT Total Amt
  - "Sales Return" ledger DEBIT Untaxed/Local Sales Amt
  - For each nonzero GST column -> DEBIT that GST ledger with its amount
- Voucher number = Invoice NO (strip leading "R" for return? keep as-is, Tally allows any string)
- Date format for Tally XML: YYYYMMDD

# Steps
1. Setup: create/activate venv, `pip install openpyxl` (Downloads path read-only via openpyxl).
2. Write script `./.autouse_verify/convert_tally.py` (build first, test) that:
   - reads both sheets via openpyxl
   - builds ENVELOPE/BODY/IMPORTDATA/REQUESTDATA with TALLYMESSAGE per voucher
   - writes final output to Downloads: "Sales and Return Register Report (22) - Tally.xml"
3. Run script, inspect row counts (~1147+409=1556 vouchers expected) and validate XML well-formed (xml.etree parse).
4. Move finalized script logic to a permanent location if needed (not required — script is throwaway, only output XML persists) then cleanup ./.autouse_verify/.

# Verification
- ./.autouse_verify/ check: parse produced XML with ElementTree, count TALLYMESSAGE / VOUCHER nodes == total data rows (excluding blanks), confirm well-formed, confirm DEBIT+CREDIT balance per voucher (sum to 0).
