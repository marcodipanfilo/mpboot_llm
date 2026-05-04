from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a small portal page that switches between the summary table site and the F1 comparison site."
    )
    parser.add_argument(
        "run_path",
        type=Path,
        help="Anchor batch directory under outputs/<system>/<batch_timestamp>",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory where the portal should be written. Defaults to outputs/summary/<system>/<timestamp>",
    )
    return parser.parse_args()


def _html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MPBootLLM Results Portal</title>
  <style>
    :root {
      --paper: #f5efe2;
      --ink: #1f1a14;
      --muted: #6c6358;
      --line: #c8baa4;
      --accent: #8a3b12;
      --panel: rgba(255, 250, 240, 0.96);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(201,110,49,0.12), transparent 30%),
        linear-gradient(180deg, #f7f1e7 0%, #f2eadc 100%);
    }
    .page {
      display: grid;
      grid-template-rows: auto 1fr;
      min-height: 100vh;
      padding: 20px 20px 16px;
      gap: 16px;
    }
    .hero {
      background: linear-gradient(135deg, rgba(255,250,240,0.92), rgba(245,239,226,0.98));
      border: 1px solid rgba(138,59,18,0.14);
      border-radius: 24px;
      padding: 20px 24px 16px;
      box-shadow: 0 18px 60px rgba(73, 48, 28, 0.08);
    }
    .eyebrow {
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-size: 12px;
      color: var(--accent);
      margin-bottom: 10px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: clamp(28px, 4vw, 46px);
      line-height: 1;
    }
    .subtitle {
      margin: 0 0 14px;
      color: var(--muted);
      max-width: 980px;
      line-height: 1.45;
      font-size: 17px;
    }
    .switcher {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }
    .switcher button {
      border: 1px solid var(--line);
      background: #fff8ee;
      color: var(--ink);
      border-radius: 999px;
      padding: 10px 16px;
      font: inherit;
      font-size: 14px;
      cursor: pointer;
      transition: background 120ms ease, border-color 120ms ease, transform 120ms ease;
    }
    .switcher button:hover {
      transform: translateY(-1px);
      border-color: rgba(138,59,18,0.32);
    }
    .switcher button.active {
      background: rgba(255, 238, 214, 0.98);
      border-color: rgba(138,59,18,0.42);
      color: var(--accent);
      font-weight: 700;
    }
    .hint {
      color: var(--muted);
      font-size: 13px;
      margin-top: 10px;
    }
    .frame-shell {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      overflow: hidden;
      box-shadow: 0 18px 60px rgba(73, 48, 28, 0.08);
      min-height: 0;
    }
    iframe {
      display: block;
      width: 100%;
      height: calc(100vh - 230px);
      border: 0;
      background: #fffaf0;
    }
    @media (max-width: 720px) {
      .page { padding: 12px; }
      iframe { height: calc(100vh - 260px); }
    }
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <div class="eyebrow">MPBootLLM · Results Portal</div>
      <h1>Switch between the detailed matrix and the grouped summary.</h1>
      <p class="subtitle">
        Both generated pages live side by side in this batch summary folder. Use the buttons below to switch views
        without leaving the current page.
      </p>
      <div class="switcher" id="switcher">
        <button type="button" data-target="rodi_f1_site_refactored/index.html" class="active">Detailed F1 Matrix</button>
        <button type="button" data-target="summary_table_site/index.html">Grouped Summary Table</button>
      </div>
      <div class="hint">The last selected view is remembered locally in this browser.</div>
    </section>

    <section class="frame-shell">
      <iframe id="view-frame" src="rodi_f1_site_refactored/index.html" title="MPBootLLM results view"></iframe>
    </section>
  </div>

  <script>
    const STORAGE_KEY = 'mpboot-summary-portal-view';
    const buttons = [...document.querySelectorAll('#switcher button')];
    const frame = document.getElementById('view-frame');

    function setActive(target) {
      buttons.forEach((button) => {
        const active = button.dataset.target === target;
        button.classList.toggle('active', active);
      });
      frame.src = target;
      try {
        localStorage.setItem(STORAGE_KEY, target);
      } catch (err) {
        console.warn(err);
      }
    }

    buttons.forEach((button) => {
      button.addEventListener('click', () => setActive(button.dataset.target));
    });

    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved && buttons.some((button) => button.dataset.target === saved)) {
        setActive(saved);
      }
    } catch (err) {
      console.warn(err);
    }
  </script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    run_path = args.run_path.resolve()
    if not run_path.exists() or not run_path.is_dir():
      raise SystemExit(f"Run path not found or not a directory: {run_path}")

    outputs_root = run_path.parents[1]
    output_dir = (args.output_dir or (outputs_root / "summary" / run_path.parent.name / run_path.name)).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    index_path = output_dir / "index.html"
    index_path.write_text(_html(), encoding="utf-8")

    print(f"Wrote {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
