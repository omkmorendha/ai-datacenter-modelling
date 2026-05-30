# AI Data-Center Macro Stress Model (v0)

A modular, scenario-based macro-financial **stress-testing tool** for the AI
data-center buildout and its interaction with U.S. Treasuries, SOFR, private
credit, securitization, hyperscaler capex, power markets, and private-market
liquidity.

> Full documentation (thesis, structure, data sources, how to run, how to add
> assumptions, limitations) is at the bottom of this file. This is a working
> Python repo, not a narrative report.

## Quick start (uses a uv-managed venv)

```bash
uv venv --python 3.11          # create .venv
uv pip install -e ".[dev]"     # install into the venv
uv run pytest                  # run tests inside the venv
uv run streamlit run src/app/streamlit_app.py
```

This is a stress-testing tool, not a forecasting oracle. Many inputs are
low-confidence scenario assumptions, not facts — see the Model Limitations
section.
