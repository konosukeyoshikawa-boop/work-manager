"""Markdown to styled HTML, then open in browser for PDF print."""
import markdown
import os

INPUT_MD = r"C:\Users\paul1\FLYCKA_越境EC戦略.md"
OUTPUT_HTML = r"C:\Users\paul1\FLYCKA_越境EC戦略.html"

with open(INPUT_MD, "r", encoding="utf-8") as f:
    md_text = f.read()

html_body = markdown.markdown(
    md_text,
    extensions=["tables", "fenced_code"],
)

full_html = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>FLYCKA 越境EC戦略書</title>
<style>
  @media print {
    @page {
      size: A4;
      margin: 18mm 15mm;
    }
    body { font-size: 10px; }
    h1 { page-break-before: auto; }
    table { page-break-inside: avoid; }
    .no-print { display: none; }
  }
  @media screen {
    body { max-width: 900px; margin: 0 auto; padding: 20px 40px; }
  }
  body {
    font-family: "BIZ UDGothic", "Yu Gothic", "Hiragino Sans", "Meiryo", sans-serif;
    font-size: 11px;
    line-height: 1.75;
    color: #1a1a1a;
  }
  h1 {
    font-size: 22px;
    border-bottom: 3px solid #2c3e50;
    padding-bottom: 8px;
    margin-top: 32px;
    margin-bottom: 14px;
    color: #2c3e50;
  }
  h2 {
    font-size: 16px;
    border-bottom: 1px solid #bdc3c7;
    padding-bottom: 5px;
    margin-top: 26px;
    margin-bottom: 10px;
    color: #2c3e50;
  }
  h3 {
    font-size: 13px;
    margin-top: 18px;
    margin-bottom: 8px;
    color: #34495e;
  }
  table {
    border-collapse: collapse;
    width: 100%;
    margin: 10px 0 14px 0;
    font-size: 10px;
  }
  th {
    background-color: #2c3e50;
    color: white;
    padding: 6px 8px;
    text-align: left;
    font-weight: 600;
    border: 1px solid #2c3e50;
  }
  td {
    border: 1px solid #ddd;
    padding: 5px 8px;
  }
  tr:nth-child(even) {
    background-color: #f8f9fa;
  }
  code {
    background-color: #f0f0f0;
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
    font-family: "Consolas", "Courier New", monospace;
  }
  pre {
    background-color: #f4f4f4;
    padding: 12px;
    border-radius: 4px;
    border: 1px solid #e0e0e0;
    font-size: 10px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-wrap: break-word;
    font-family: "Consolas", "Courier New", monospace;
  }
  pre code {
    background: none;
    padding: 0;
  }
  strong {
    color: #c0392b;
  }
  ul, ol {
    padding-left: 22px;
  }
  li {
    margin-bottom: 3px;
  }
  hr {
    border: none;
    border-top: 1px solid #ddd;
    margin: 20px 0;
  }
  p {
    margin: 6px 0;
  }
  .print-btn {
    position: fixed;
    top: 15px;
    right: 20px;
    background: #2c3e50;
    color: white;
    border: none;
    padding: 10px 24px;
    font-size: 14px;
    border-radius: 4px;
    cursor: pointer;
    z-index: 1000;
    font-family: inherit;
  }
  .print-btn:hover {
    background: #34495e;
  }
</style>
</head>
<body>
<button class="print-btn no-print" onclick="window.print()">PDF保存 (Ctrl+P)</button>
""" + html_body + """
<div style="text-align:center; margin-top:30px; font-size:9px; color:#999;">
  FLYCKA 越境EC戦略書 | 作成日: 2026-03-11
</div>
</body>
</html>"""

with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
    f.write(full_html)

print(f"HTML generated: {OUTPUT_HTML}")
print("Opening in browser... Use Ctrl+P or the button to save as PDF.")
os.startfile(OUTPUT_HTML)
